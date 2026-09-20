import Foundation

/// What Play Live asks of Motet's API. The API mints the session — it builds the episode's
/// context from the database and calls the voice service server-to-server with a start
/// token this app never holds (invariant 2) — and hands back a socket URL and the frame
/// to open it with.
public protocol VoiceAPI: Sendable {
    /// Whether this deployment has a voice service at all. "Not configured" is an answer.
    func voiceStatus() async throws -> VoiceStatusResponse
    func startVoiceSession(episodeId: String, spokenThroughMs: Int) async throws -> VoiceSessionResponse
}

extension MotetHTTPClient: VoiceAPI {
    public func voiceStatus() async throws -> VoiceStatusResponse {
        try await send(MotetEndpoints.voiceStatus, as: VoiceStatusResponse.self)
    }

    public func startVoiceSession(episodeId: String, spokenThroughMs: Int) async throws -> VoiceSessionResponse {
        try await send(
            MotetEndpoints.startVoiceSession(episodeId: episodeId),
            body: StartVoiceSessionRequest(spokenThroughMs: max(0, spokenThroughMs)),
            as: VoiceSessionResponse.self
        )
    }
}

/// The session socket, behind a protocol so the rules below are tested without a network.
///
/// Sends are synchronous and ordered — `URLSessionWebSocketTask` queues in call order — so
/// the microphone can hand frames straight to it from the audio thread. Events come back as
/// one ordered stream; an unstructured `Task` per message would not keep them in order.
public protocol LiveTransport: AnyObject, Sendable {
    func open(url: URL) -> AsyncStream<LiveTransportEvent>
    func sendText(_ text: String)
    func sendAudio(_ data: Data)
    func close()
}

public enum LiveTransportEvent: Equatable, Sendable {
    case text(String)
    case closed(code: Int)
    case failed(String)
}

/// The phone's audio for Play Live: the open microphone, and the replies.
///
/// The narration is not here. It is the rendered episode the player already holds and plays
/// locally; only the interaction is live (AGENTS.md, "Two audio paths").
public protocol LiveAudio: Sendable {
    /// Open the mic. `frames` receives 16 kHz mono int16 as it is captured, on whatever
    /// thread the audio arrives on; `level` receives dBFS now and then, for the meter.
    ///
    /// `failed` is for the mic dying *after* it opened — a route change the engine could not
    /// come back from, say — so the session can end and say so rather than go quietly deaf.
    func startCapture(
        frames: @escaping @Sendable (Data) -> Void,
        level: @escaping @Sendable (Double) -> Void,
        failed: @escaping @Sendable (String) -> Void
    ) async throws
    func stopCapture() async
    /// One chunk of a streamed reply, queued behind the last one.
    func enqueue(pcm16: Data, sampleRate: Int) async
    /// A whole reply in one container. Returns when it stops: true when it played to its
    /// end, false when `flushReplies` cut it off. Counted in `pendingReplySeconds`.
    func playContainer(_ data: Data) async throws -> Bool
    /// Stop every queued reply — the listener talked over it, or it errored.
    func flushReplies() async
    /// How much reply audio is still to be heard.
    func pendingReplySeconds() async -> Double
}

/// What the Play Live controls render.
public struct LiveSnapshot: Equatable, Sendable {
    public enum Phase: String, Equatable, Sendable {
        case idle, connecting, narrating, paused, listening, replying, resuming, error
    }

    public enum Availability: Equatable, Sendable {
        case checking
        case available
        case unavailable(String)
    }

    public struct Line: Equatable, Sendable, Identifiable {
        public enum Kind: Equatable, Sendable { case user, assistant, tool, event }
        public let id: Int
        public let kind: Kind
        public let text: String
    }

    public var availability: Availability = .checking
    public var phase: Phase = .idle
    /// Which arm is answering, echoed for the log — informational, never branched on.
    public var arm = ""
    /// Whether a speech-to-speech channel is open: the listener can just talk.
    public var isLive = false
    /// Why the live channel is not there, when the arm offered one — a short code the UI
    /// branches on (`insufficient_quota`, `arm_dormant`, …).
    public var liveUnavailable: String?
    /// The service's own sentence behind that code. Carried because `arm_dormant` on an arm
    /// that offers no live channel at all means *nothing* can answer — not even a typed
    /// question — and the only thing that says which vendor is missing is this text.
    public var liveUnavailableDetail: String?

    /// Whether this session can produce a reply of any kind — the service's own answer.
    ///
    /// False is the state production has been in since the voice service was deployed: the
    /// composed arm with no speech-to-text vendor provisioned. Barge-in works, every mic
    /// frame is forwarded, and no answer is possible — "the VAD seems to work but I'm not
    /// getting any audio". A session that cannot answer must say so where the mic is,
    /// rather than looking like one that is merely quiet.
    ///
    /// **Read from the frame, never inferred from `liveUnavailable`.** `arm_dormant` is
    /// emitted for two opposite situations — a live channel that would not open on an arm
    /// whose typed questions still work, and an arm that can reply to nothing — so a client
    /// branching on the code tells half of them the wrong thing. True by default, which is
    /// what a service too old to send the field means and the safer way to be wrong: it
    /// offers a control that might not work rather than hiding one that does.
    public var canAnswer = true
    public var lines: [Line] = []
    /// What was playing at the last barge-in.
    public var lastInterrupt: String?
    public var error: String?
    /// The last turn's failure. Not a closed socket: the next question still goes.
    public var turnError: String?
    public var micDbfs: Double = -100

    public var isRunning: Bool { phase != .idle && phase != .error }

    public var phaseLabel: String {
        switch phase {
        case .idle: return "Idle"
        case .connecting: return "Connecting…"
        case .narrating: return isLive ? "Narrating — just ask: talk over it" : "Narrating — talk over it to interrupt"
        case .paused: return "Paused — press play to carry on"
        case .listening: return "Listening — narration paused, ask your question"
        case .replying: return "Replying…"
        case .resuming: return "Resuming narration"
        case .error: return "Error"
        }
    }

    public init() {}
}

/// Play Live — listen to a rendered episode and interrupt it by voice (motet#93), on the
/// phone. A port of the SPA's `Live.tsx`, and the same rules: the voice service never
/// streams the episode; this tells it whether narration is playing and where
/// (`narration_delivered`, `narration_paused`, `narration_resumed`, `playback_position`),
/// and sends listener audio for the service's own detector to decide a barge-in on.
///
/// Everything that decides anything is here, in MotetKit, and tested on Linux against
/// fakes; the socket, the microphone and the reply player are `MotetPlayback`'s.
public actor LiveSession {
    public typealias Sleeper = @Sendable (Duration) async throws -> Void

    private let api: any VoiceAPI
    private let narration: any NarrationControl
    private let audio: any LiveAudio
    private let makeTransport: @Sendable () -> any LiveTransport
    private let sleep: Sleeper

    private var state = LiveSnapshot()
    private var observers: [UUID: AsyncStream<LiveSnapshot>.Continuation] = [:]

    private var transport: (any LiveTransport)?
    private var gate: LiveAudioGate?
    private var consumer: Task<Void, Never>?
    private var resumeTimer: Task<Void, Never>?
    /// Which Play Live this is. Stop and a new start move it on, so a start still awaiting
    /// the mint or the mic finds out it was abandoned instead of opening a socket.
    private var generation = 0
    private var readySeen = false
    /// Set when this session paused narration itself, so the pause it causes is not
    /// reported as the listener's.
    private var pausedByUs = false
    private var lastPositionSent = 0
    private var durationMs = 0
    private var nextLineId = 0

    /// How long after a streamed reply's last chunk narration picks up, on top of what is
    /// still queued. The SPA's 150 ms.
    static let resumeSlack: Duration = .milliseconds(150)
    /// The transcript the screen keeps. A long session is not a memory leak.
    static let maxLines = 200

    public init(
        api: any VoiceAPI,
        narration: any NarrationControl,
        audio: any LiveAudio,
        makeTransport: @escaping @Sendable () -> any LiveTransport,
        sleep: @escaping Sleeper = { try await Task.sleep(for: $0) }
    ) {
        self.api = api
        self.narration = narration
        self.audio = audio
        self.makeTransport = makeTransport
        self.sleep = sleep
    }

    // MARK: - State out

    public func snapshot() -> LiveSnapshot { state }

    public func snapshots() -> AsyncStream<LiveSnapshot> {
        AsyncStream { continuation in
            let id = UUID()
            observers[id] = continuation
            continuation.yield(state)
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeObserver(id) }
            }
        }
    }

    private func removeObserver(_ id: UUID) { observers[id] = nil }

    private func publish() {
        for continuation in observers.values { continuation.yield(state) }
    }

    private func setPhase(_ phase: LiveSnapshot.Phase) {
        state.phase = phase
        publish()
    }

    private func push(_ kind: LiveSnapshot.Line.Kind, _ text: String) {
        nextLineId += 1
        state.lines.append(.init(id: nextLineId, kind: kind, text: text))
        if state.lines.count > Self.maxLines { state.lines.removeFirst(state.lines.count - Self.maxLines) }
        publish()
    }

    // MARK: - Availability

    /// Asked of *our* API, which exists in every environment — never of the voice service,
    /// which does not exist in most of them.
    public func checkAvailability() async {
        do {
            let status = try await api.voiceStatus()
            state.availability = status.configured
                ? .available
                : .unavailable(status.reason ?? "Live voice isn’t configured in this environment.")
        } catch {
            state.availability = .unavailable("Could not ask the API whether live voice is available.")
        }
        publish()
    }

    // MARK: - Starting and stopping

    public func start(episodeId: String, durationMs: Int) async {
        guard !state.isRunning else { return }
        let availability = state.availability
        state = LiveSnapshot()
        state.availability = availability
        readySeen = false
        pausedByUs = false
        lastPositionSent = 0
        self.durationMs = durationMs
        generation += 1
        let mine = generation
        setPhase(.connecting)
        await audio.flushReplies()
        do {
            let position = await narration.narrationContext().positionMs
            let session = try await api.startVoiceSession(episodeId: episodeId, spokenThroughMs: position)
            guard generation == mine else { return }
            state.arm = session.arm + (session.conversational ? "" : " (text turns only)")
            guard let url = URL(string: session.websocketUrl),
                  let authenticate = String(data: try JSONEncoder().encode(session.authenticateFrame), encoding: .utf8)
            else { throw LiveFailure.badSession }

            let transport = makeTransport()
            let gate = LiveAudioGate(transport: transport)
            try await audio.startCapture(
                frames: { data in gate.forward(data) },
                level: { [weak self] db in Task { await self?.meter(db) } },
                failed: { [weak self] message in Task { await self?.audioFailed(message, generation: mine) } }
            )
            guard generation == mine else {
                await audio.stopCapture()
                return
            }
            self.transport = transport
            self.gate = gate
            let events = transport.open(url: url)
            // The first frame must be `authenticate`; the gate holds listener audio until
            // it has been queued, and the socket keeps the order it was handed.
            transport.sendText(authenticate)
            gate.open()
            consumer = Task { [weak self] in
                for await event in events {
                    await self?.handle(event, generation: mine)
                }
            }
        } catch {
            guard generation == mine else { return }
            await teardown()
            state.error = Self.describe(error)
            setPhase(.error)
        }
    }

    /// Stop Live: tell the service, pause the briefing, and let go of everything.
    ///
    /// `pauseNarration: false` is for the session ending because another episode was loaded:
    /// pausing then would pause the *new* episode, which is not what anyone asked for.
    public func stop(pauseNarration: Bool = true) async {
        send(.close)
        pausedByUs = true
        await teardown()
        if pauseNarration {
            _ = await narration.suspendNarration()
        }
    }

    private func audioFailed(_ message: String, generation mine: Int) async {
        guard generation == mine, state.isRunning else { return }
        send(.close)
        await teardown()
        state.error = message
        setPhase(.error)
    }

    private func teardown() async {
        generation += 1
        clearResumeTimer()
        consumer?.cancel()
        consumer = nil
        gate?.shut()
        gate = nil
        transport?.close()
        transport = nil
        await audio.stopCapture()
        await audio.flushReplies()
        setPhase(.idle)
    }

    // MARK: - The listener

    /// "Just ask", pressed rather than spoken. Counted as a barge-in like any other.
    public func interrupt() {
        send(.bargeIn)
    }

    public func ask(_ question: String) {
        let text = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, state.isRunning else { return }
        state.turnError = nil
        // On the live channel the service echoes what it heard; on the typed path the
        // question is shown here, once.
        if !state.isLive { push(.user, text) }
        setPhase(.replying)
        send(.text(text))
    }

    /// "Never mind, resume" — and what a finished reply does.
    ///
    /// Every await here can let another event in — a barge-in, a Stop — so the phase and the
    /// session are checked again after each one rather than assumed.
    public func resumeNarration() async {
        guard transport != nil, state.phase == .listening || state.phase == .replying else { return }
        let mine = generation
        clearResumeTimer()
        setPhase(.resuming)
        // Resume from the interruption offset, deliberately not rewound: whether a couple of
        // seconds of rewind helps is an open question for the owner (motet#93).
        let position = await narration.narrationContext().positionMs
        guard generation == mine, state.phase == .resuming else { return }
        send(.narrationResumed(spokenThroughMs: position))
        pausedByUs = false
        await narration.resumeNarration()
        guard generation == mine else { return }
        if state.phase == .resuming { setPhase(.narrating) }
    }

    // MARK: - The player

    /// The player started or stopped, from its snapshots. A pause the listener made is
    /// `narration_paused`; pressing play again is `narration_resumed`. Neither is a barge-in.
    public func narrationChanged(isPlaying: Bool, positionMs: Int) async {
        guard state.isRunning, state.phase != .connecting else { return }
        if isPlaying {
            switch state.phase {
            case .listening, .replying:
                // The listener pressed play mid-exchange. That is a resume, and the service
                // has to hear it, or its clock stays frozen and the mic keeps going to the
                // voice provider while the briefing plays.
                clearResumeTimer()
                await audio.flushReplies()
                pausedByUs = false
            case .paused:
                break
            default:
                return
            }
            send(.narrationResumed(spokenThroughMs: positionMs))
            setPhase(.narrating)
        } else {
            guard !pausedByUs, state.phase == .narrating else { return }
            send(.narrationPaused(spokenThroughMs: positionMs))
            setPhase(.paused)
        }
    }

    /// Where the player is, about once a second.
    public func narrationPosition(_ positionMs: Int) {
        guard state.isRunning, state.phase != .connecting else { return }
        guard abs(positionMs - lastPositionSent) >= 1_000 else { return }
        lastPositionSent = positionMs
        send(.playbackPosition(spokenThroughMs: positionMs))
    }

    private func meter(_ dbfs: Double) {
        guard state.isRunning else { return }
        state.micDbfs = dbfs
        publish()
    }

    // MARK: - The service

    func handle(_ event: LiveTransportEvent, generation mine: Int) async {
        guard generation == mine else { return }
        switch event {
        case .text(let text):
            do {
                await handle(try LiveEvent.decode(text), generation: mine)
            } catch {
                push(.event, "Unreadable event from the voice service.")
            }
        case .failed(let message):
            await teardown()
            state.error = "The voice socket failed: \(message)"
            setPhase(.error)
        case .closed(let code):
            let wasRunning = state.isRunning
            await teardown()
            if wasRunning { push(.event, "Socket closed (\(code)).") }
        }
    }

    private func handle(_ event: LiveEvent, generation mine: Int) async {
        switch event {
        case .sessionState(let phase, let detail, let reason, let live, let canAnswer):
            switch phase {
            case "ready":
                if !readySeen {
                    // The first `ready` opens the session; narration starts.
                    readySeen = true
                    let opened = live ?? (detail?.hasPrefix("live conversation open") ?? false)
                    state.isLive = opened
                    if let canAnswer { state.canAnswer = canAnswer }
                    if !opened, let reason {
                        state.liveUnavailable = reason
                        state.liveUnavailableDetail = detail
                    }
                    if let detail { push(.event, "Ready · \(detail)") }
                    await beginNarration(generation: mine)
                } else {
                    // Every later `ready` is a reply that has finished streaming: let the
                    // queued audio drain, then narration picks up where it stopped.
                    let remaining = await audio.pendingReplySeconds()
                    guard generation == mine else { return }
                    scheduleResume(after: .milliseconds(Int(remaining * 1_000)) + Self.resumeSlack)
                }
            case "listening":
                // The service engaged the live channel, or the listener talked over a reply
                // and the service cut it off — drop whatever is queued and listen.
                await audio.flushReplies()
                guard generation == mine else { return }
                if let live { state.isLive = live }
                setPhase(.listening)
                if let detail, detail != "live — speak your question" { push(.event, detail) }
            case "speaking":
                setPhase(.replying)
            case "closed":
                await teardown()
            default:
                break
            }
        case .interruptedAt(let offsetMs, let segmentTitle, let claimText, _):
            pausedByUs = true
            clearResumeTimer()
            _ = await narration.suspendNarration()
            guard generation == mine else { return }
            var line = String(format: "Interrupted at %.1fs", Double(offsetMs) / 1_000)
            if let segmentTitle {
                line += " during “\(segmentTitle)”"
                if let claimText { line += " — “\(claimText)”" }
            }
            state.lastInterrupt = segmentTitle.map { title in claimText.map { "\(title) — “\($0)”" } ?? title }
            setPhase(.listening)
            push(.event, line)
        case .transcript(let speaker, let text, _):
            if speaker == "assistant" { push(.assistant, text) }
            else if state.isLive { push(.user, text) }
        case .audioChunk(let data, let sampleRate, _, let format):
            if format == "pcm16" {
                await audio.enqueue(pcm16: data, sampleRate: sampleRate)
            } else {
                // A whole reply in one container (the composed arm): play it, then resume.
                // Not awaited here — the events behind it, a barge-in among them, must not
                // queue up behind a reply that is still playing.
                let audio = self.audio
                let mine = generation
                Task { [weak self] in
                    let finished: Bool
                    do { finished = try await audio.playContainer(data) } catch {
                        await self?.containerFailed(generation: mine)
                        return
                    }
                    if finished { await self?.containerFinished(generation: mine) }
                }
            }
        case .toolCall(let name, let arguments):
            push(.tool, "→ \(name)(\(arguments))")
        case .toolResult(let name, let ok, let error):
            push(.tool, "← \(name): \(ok ? "ok" : "failed — \(error ?? "")")")
        case .error(let code, let message):
            if code == "turn_failed" || code == "arm_dormant" {
                // One reply failed. Not a closed socket: back to listening, the error under
                // the question box, and the box still takes the next question.
                state.turnError = "\(code): \(message)"
                await audio.flushReplies()
                guard generation == mine else { return }
                setPhase(.listening)
                return
            }
            if code == "live_unavailable" {
                // The live channel died mid-session. Typed questions go to the text arm.
                state.isLive = false
                state.liveUnavailable = code
            }
            if state.phase == .replying {
                // No `ready` is coming for a reply that errored.
                await audio.flushReplies()
                guard generation == mine else { return }
                setPhase(.listening)
            }
            push(.event, "Error \(code): \(message)")
        case .other:
            break
        }
    }

    private func containerFinished(generation mine: Int) async {
        guard generation == mine else { return }
        await resumeNarration()
    }

    private func containerFailed(generation mine: Int) async {
        guard generation == mine else { return }
        push(.event, "Could not play the reply audio.")
        await resumeNarration()
    }

    private func beginNarration(generation mine: Int) async {
        send(.narrationDelivered(durationMs: durationMs))
        let position = await narration.narrationContext().positionMs
        guard generation == mine else { return }
        lastPositionSent = position
        send(.playbackPosition(spokenThroughMs: position))
        pausedByUs = false
        await narration.resumeNarration()
        // Stopped while narration was starting: nothing to do here. A Stop pauses after its
        // teardown, which lands behind this resume; an episode change deliberately does not.
        guard generation == mine else { return }
        setPhase(.narrating)
    }

    private func scheduleResume(after delay: Duration) {
        clearResumeTimer()
        let sleep = self.sleep
        resumeTimer = Task { [weak self] in
            do { try await sleep(delay) } catch { return }
            await self?.resumeNarration()
        }
    }

    private func clearResumeTimer() {
        resumeTimer?.cancel()
        resumeTimer = nil
    }

    private func send(_ frame: LiveFrame) {
        transport?.sendText(frame.json)
    }

    enum LiveFailure: Error, CustomStringConvertible {
        case badSession
        var description: String { "The API answered with a voice session this app cannot open." }
    }

    static func describe(_ error: Error) -> String {
        if let error = error as? MotetError {
            if case .http(503, let detail?) = error { return detail }
            if case .http(_, let detail?) = error { return detail }
            return error.description
        }
        if let error = error as? LiveFailure { return error.description }
        let text = String(describing: error)
        return text.isEmpty ? "Play Live could not start." : text
    }
}

/// Holds listener audio back until the `authenticate` frame has been queued, and drops it
/// once the session is gone. The one piece of Play Live the audio thread touches directly.
final class LiveAudioGate: @unchecked Sendable {
    private let lock = NSLock()
    private weak var transport: (any LiveTransport)?
    private var isOpen = false

    init(transport: any LiveTransport) {
        self.transport = transport
    }

    func open() { lock.withLock { isOpen = true } }
    func shut() { lock.withLock { isOpen = false; transport = nil } }

    func forward(_ data: Data) {
        let target: (any LiveTransport)? = lock.withLock { isOpen ? transport : nil }
        target?.sendAudio(data)
    }
}
