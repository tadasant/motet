import XCTest
@testable import MotetKit

// MARK: - Doubles

actor FakeVoiceAPI: VoiceAPI {
    var status = VoiceStatusResponse(configured: true)
    var failure: MotetError?
    private(set) var minted: [Int] = []
    /// Hold every mint until the gate opens, to stop a session mid-mint.
    private var gated = false
    private var held: [CheckedContinuation<Void, Never>] = []

    func setGate(_ closed: Bool) {
        gated = closed
        if !closed {
            held.forEach { $0.resume() }
            held = []
        }
    }

    func setStatus(_ status: VoiceStatusResponse) { self.status = status }
    func setFailure(_ failure: MotetError?) { self.failure = failure }

    func voiceStatus() async throws -> VoiceStatusResponse { status }

    func startVoiceSession(episodeId: String, spokenThroughMs: Int) async throws -> VoiceSessionResponse {
        minted.append(spokenThroughMs)
        if gated { await withCheckedContinuation { held.append($0) } }
        if let failure { throw failure }
        return VoiceSessionResponse(
            arm: "openai_realtime",
            authenticateFrame: .object(["type": .string("authenticate"), "token": .string("tok")]),
            conversational: true,
            expiresAt: "2026-09-19T10:00:00Z",
            sessionId: "vs_1",
            sessionToken: "tok",
            websocketUrl: "wss://voice.example.invalid/v1/sessions/vs_1/ws"
        )
    }
}

final class FakeTransport: LiveTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncStream<LiveTransportEvent>.Continuation?
    private var _texts: [String] = []
    private var _audio: [Data] = []
    private(set) var openedURL: URL?
    private(set) var closed = false

    var texts: [String] { lock.withLock { _texts } }
    var audio: [Data] { lock.withLock { _audio } }
    /// The frame types sent, in order.
    var types: [String] {
        texts.compactMap { text in
            (try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any])?["type"] as? String
        }
    }

    func frames(_ type: String) -> [[String: Any]] {
        texts.compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            .filter { $0["type"] as? String == type }
    }

    func open(url: URL) -> AsyncStream<LiveTransportEvent> {
        openedURL = url
        return AsyncStream { continuation in lock.withLock { self.continuation = continuation } }
    }

    func sendText(_ text: String) { lock.withLock { _texts.append(text) } }
    func sendAudio(_ data: Data) { lock.withLock { _audio.append(data) } }
    func close() { closed = true; lock.withLock { continuation?.finish() } }

    func receive(_ json: String) { _ = lock.withLock { continuation }?.yield(.text(json)) }
    func drop(code: Int) { _ = lock.withLock { continuation }?.yield(.closed(code: code)) }
}

actor FakeLiveAudio: LiveAudio {
    private(set) var capturing = false
    private(set) var enqueued: [Int] = []
    private(set) var flushes = 0
    private(set) var containers = 0
    var pending: Double = 0
    var captureError: Error?
    private var frames: (@Sendable (Data) -> Void)?
    private var failed: (@Sendable (String) -> Void)?

    func setCaptureError(_ error: Error?) { captureError = error }

    func startCapture(
        frames: @escaping @Sendable (Data) -> Void,
        level: @escaping @Sendable (Double) -> Void,
        failed: @escaping @Sendable (String) -> Void
    ) async throws {
        if let captureError { throw captureError }
        capturing = true
        self.frames = frames
        self.failed = failed
    }

    /// The engine dying under a running session — a route change it could not survive.
    func die(_ message: String) { failed?(message) }
    func stopCapture() async { capturing = false }
    func enqueue(pcm16: Data, sampleRate: Int) async { enqueued.append(pcm16.count) }
    func playContainer(_ data: Data) async throws -> Bool { containers += 1; return true }
    func flushReplies() async { flushes += 1 }
    func pendingReplySeconds() async -> Double { pending }

    /// A mic frame, as the audio thread would deliver it.
    func speak(_ data: Data) { frames?(data) }
}

actor FakeNarration: NarrationControl {
    var position = 42_000
    private(set) var playing = false
    private(set) var plays = 0
    private(set) var pauses = 0

    func suspendNarration() async -> Int { playing = false; pauses += 1; return position }
    func resumeNarration() async { playing = true; plays += 1 }
    func narrationContext() async -> NarrationContext {
        NarrationContext(episodeId: "ep-1", positionMs: position, currentNewsItemId: nil, currentNewsItemTitle: nil)
    }
}

// MARK: - Tests

final class LiveSessionTests: XCTestCase {
    private struct Harness {
        let session: LiveSession
        let api: FakeVoiceAPI
        let transport: FakeTransport
        let audio: FakeLiveAudio
        let narration: FakeNarration
    }

    private func makeHarness() -> Harness {
        let api = FakeVoiceAPI()
        let transport = FakeTransport()
        let audio = FakeLiveAudio()
        let narration = FakeNarration()
        let session = LiveSession(
            api: api, narration: narration, audio: audio,
            makeTransport: { transport },
            sleep: { _ in }
        )
        return Harness(session: session, api: api, transport: transport, audio: audio, narration: narration)
    }

    private func started(_ h: Harness, readyJSON: String = #"{"type":"session_state","at_ms":0,"state":"ready","detail":"live conversation open","live":true}"#) async throws {
        await h.session.start(episodeId: "ep-1", durationMs: 300_000)
        h.transport.receive(readyJSON)
        try await waitUntil { await h.session.snapshot().phase == .narrating }
    }

    func testTheSessionIsMintedAtTheListenersPositionAndAuthenticatesFirst() async throws {
        let h = makeHarness()
        await h.audio.speak(Data([1, 2]))  // before anything is open: dropped
        try await started(h)

        let minted = await h.api.minted
        XCTAssertEqual(minted, [42_000])
        XCTAssertEqual(h.transport.openedURL?.absoluteString, "wss://voice.example.invalid/v1/sessions/vs_1/ws")
        XCTAssertEqual(h.transport.types.first, "authenticate")
        XCTAssertEqual(Array(h.transport.types.dropFirst()), ["narration_delivered", "playback_position"])
        XCTAssertEqual(h.transport.frames("narration_delivered").first?["duration_ms"] as? Int, 300_000)
        let playing = await h.narration.playing
        XCTAssertTrue(playing, "the first ready starts narration")
        let live = await h.session.snapshot().isLive
        XCTAssertTrue(live)
    }

    func testListenerAudioFlowsOnlyOnceTheSocketIsAuthenticated() async throws {
        let h = makeHarness()
        try await started(h)
        await h.audio.speak(Data([7, 7]))
        XCTAssertEqual(h.transport.audio, [Data([7, 7])])

        await h.session.stop()
        await h.audio.speak(Data([8, 8]))
        XCTAssertEqual(h.transport.audio.count, 1, "nothing leaves after Stop")
        XCTAssertTrue(h.transport.closed)
        XCTAssertEqual(h.transport.types.last, "close")
    }

    func testABargeInPausesNarrationAndItsOwnPauseIsNotReportedAsTheListeners() async throws {
        let h = makeHarness()
        try await started(h)

        h.transport.receive(#"{"type":"interrupted_at","at_ms":5,"offset_ms":61200,"decision":{"trigger":"vad"},"context":{"segment_title":"Alpha","claim_text":"They raised twelve million."}}"#)
        try await waitUntil { await h.session.snapshot().phase == .listening }
        await h.session.narrationChanged(isPlaying: false, positionMs: 61_200)

        XCTAssertTrue(h.transport.frames("narration_paused").isEmpty)
        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.lastInterrupt, "Alpha — “They raised twelve million.”")
        XCTAssertEqual(snapshot.lines.last?.text, "Interrupted at 61.2s during “Alpha” — “They raised twelve million.”")
    }

    func testAFinishedReplyResumesNarrationFromWhereItStopped() async throws {
        let h = makeHarness()
        try await started(h)
        h.transport.receive(#"{"type":"interrupted_at","at_ms":5,"offset_ms":61200}"#)
        h.transport.receive(#"{"type":"session_state","at_ms":6,"state":"speaking"}"#)
        h.transport.receive(#"{"type":"audio_chunk","at_ms":7,"pcm_base64":"AAAAAA==","sample_rate":24000,"duration_ms":1,"format":"pcm16"}"#)
        try await waitUntil { await h.audio.enqueued == [4] }
        await h.narration.setPosition(61_200)

        h.transport.receive(#"{"type":"session_state","at_ms":8,"state":"ready"}"#)
        try await waitUntil { await h.session.snapshot().phase == .narrating }

        XCTAssertEqual(h.transport.frames("narration_resumed").first?["spoken_through_ms"] as? Int, 61_200)
        let plays = await h.narration.plays
        XCTAssertEqual(plays, 2)
    }

    func testAPauseTheListenerMakesIsReportedAndPlayResumes() async throws {
        let h = makeHarness()
        try await started(h)

        await h.session.narrationChanged(isPlaying: false, positionMs: 70_000)
        let paused = await h.session.snapshot().phase
        XCTAssertEqual(paused, .paused)
        XCTAssertEqual(h.transport.frames("narration_paused").first?["spoken_through_ms"] as? Int, 70_000)

        await h.session.narrationChanged(isPlaying: true, positionMs: 70_000)
        let resumed = await h.session.snapshot().phase
        XCTAssertEqual(resumed, .narrating)
        XCTAssertEqual(h.transport.frames("narration_resumed").count, 1)
        XCTAssertEqual(h.transport.types.filter { $0 == "barge_in" }, [], "neither is a barge-in")
    }

    func testPressingPlayMidExchangeTellsTheServiceSoTheMicGateCloses() async throws {
        let h = makeHarness()
        try await started(h)
        h.transport.receive(#"{"type":"interrupted_at","at_ms":5,"offset_ms":1000}"#)
        try await waitUntil { await h.session.snapshot().phase == .listening }

        await h.session.narrationChanged(isPlaying: true, positionMs: 1_000)

        XCTAssertEqual(h.transport.frames("narration_resumed").count, 1)
        let phase = await h.session.snapshot().phase
        XCTAssertEqual(phase, .narrating)
    }

    func testPositionIsReportedAboutOnceASecond() async throws {
        let h = makeHarness()
        try await started(h)
        let before = h.transport.frames("playback_position").count
        await h.session.narrationPosition(42_500)
        await h.session.narrationPosition(43_100)
        await h.session.narrationPosition(44_200)
        XCTAssertEqual(h.transport.frames("playback_position").count - before, 2)
    }

    func testATypedQuestionIsShownOnceAndSent() async throws {
        let h = makeHarness()
        try await started(h, readyJSON: #"{"type":"session_state","at_ms":0,"state":"ready","detail":"composed","reason":"insufficient_quota","live":false}"#)

        await h.session.ask("  what was that number?  ")

        XCTAssertEqual(h.transport.frames("text").first?["text"] as? String, "what was that number?")
        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.phase, .replying)
        XCTAssertEqual(snapshot.lines.last?.kind, .user)
        XCTAssertEqual(snapshot.liveUnavailable, "insufficient_quota")
    }

    func testADormantArmSaysNothingCanAnswerRatherThanOfferingTypedQuestions() async throws {
        let h = makeHarness()
        // What production has sent since the voice service was deployed: `arm=composed`
        // with no speech-to-text vendor provisioned. Barge-in works and every mic frame is
        // forwarded, which is why it reads as "the VAD works but I get no audio".
        try await started(
            h,
            readyJSON: #"{"type":"session_state","at_ms":0,"state":"ready","detail":"no speech-to-text vendor is provisioned for the composed arm","reason":"arm_dormant","live":false,"can_answer":false}"#
        )

        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.liveUnavailable, "arm_dormant")
        XCTAssertFalse(snapshot.canAnswer, "a dormant arm cannot answer a typed question either")
        XCTAssertEqual(
            snapshot.liveUnavailableDetail,
            "no speech-to-text vendor is provisioned for the composed arm"
        )
    }

    func testALiveChannelThatDidNotOpenStillAnswersTypedQuestions() async throws {
        let h = makeHarness()
        try await started(
            h,
            readyJSON: #"{"type":"session_state","at_ms":0,"state":"ready","detail":"composed","reason":"insufficient_quota","live":false,"can_answer":true}"#
        )

        // The distinction the screen's two sentences rest on: out of credits is not the
        // same as nothing in the process being able to reply.
        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.liveUnavailable, "insufficient_quota")
        XCTAssertTrue(snapshot.canAnswer)
    }

    func testACodeAloneIsNotEnoughBecauseArmDormantMeansTwoOppositeThings() async throws {
        let h = makeHarness()
        // The same `arm_dormant` code, from a LiveArm whose channel would not open — where
        // `text_arm` does answer a typed question. A client branching on the code alone
        // would tell this listener their working question box is dead.
        try await started(
            h,
            readyJSON: #"{"type":"session_state","at_ms":0,"state":"ready","detail":"live conversation unavailable (arm_dormant); answering typed questions with the composed arm","reason":"arm_dormant","live":false,"can_answer":true}"#
        )

        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.liveUnavailable, "arm_dormant")
        XCTAssertTrue(snapshot.canAnswer)
    }

    func testAServiceTooOldToSendCanAnswerIsAssumedAbleTo() async throws {
        let h = makeHarness()
        try await started(
            h,
            readyJSON: #"{"type":"session_state","at_ms":0,"state":"ready","detail":"composed","reason":"insufficient_quota","live":false}"#
        )

        // Offering a control that might not work beats hiding one that does.
        let snapshot = await h.session.snapshot()
        XCTAssertTrue(snapshot.canAnswer)
    }

    func testAFailedTurnIsNotAClosedSocket() async throws {
        let h = makeHarness()
        try await started(h)
        await h.session.ask("hello")
        h.transport.receive(#"{"type":"error","at_ms":9,"code":"turn_failed","message":"vendor refused"}"#)
        try await waitUntil { await h.session.snapshot().phase == .listening }

        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.turnError, "turn_failed: vendor refused")
        XCTAssertTrue(snapshot.isRunning)
        XCTAssertFalse(h.transport.closed)
    }

    func testTheServiceClosingEndsTheSessionAndReleasesTheMic() async throws {
        let h = makeHarness()
        try await started(h)

        h.transport.drop(code: 1011)
        try await waitUntil { await h.session.snapshot().phase == .idle }

        let capturing = await h.audio.capturing
        XCTAssertFalse(capturing)
        let lines = await h.session.snapshot().lines
        XCTAssertEqual(lines.last?.text, "Socket closed (1011).")
    }

    func testAnUnconfiguredDeploymentSaysWhyAndMintsNothing() async throws {
        let h = makeHarness()
        await h.api.setStatus(VoiceStatusResponse(configured: false, reason: "MOTET_VOICE_BASE_URL is not set."))
        await h.session.checkAvailability()
        let availability = await h.session.snapshot().availability
        XCTAssertEqual(availability, .unavailable("MOTET_VOICE_BASE_URL is not set."))
    }

    func testAMicThatWillNotOpenIsAnErrorAndLeavesNothingRunning() async throws {
        let h = makeHarness()
        await h.audio.setCaptureError(MotetError.http(status: 0, detail: "Microphone access is off."))
        await h.session.start(episodeId: "ep-1", durationMs: 300_000)

        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.phase, .error)
        XCTAssertEqual(snapshot.error, "Microphone access is off.")
        XCTAssertNil(h.transport.openedURL, "no socket without a mic")
    }

    func testStoppingWhileTheSessionIsBeingMintedOpensNothing() async throws {
        let h = makeHarness()
        await h.api.setGate(true)
        let starting = Task { await h.session.start(episodeId: "ep-1", durationMs: 300_000) }
        for _ in 0..<2_000 {
            if await h.api.minted.count == 1 { break }
            try await Task.sleep(for: .milliseconds(1))
        }
        await h.session.stop()
        await h.api.setGate(false)
        await starting.value

        XCTAssertNil(h.transport.openedURL, "a start abandoned mid-mint opens no socket")
        let capturing = await h.audio.capturing
        XCTAssertFalse(capturing)
        let phase = await h.session.snapshot().phase
        XCTAssertEqual(phase, .idle)
    }

    func testAComposedReplyResumesNarrationOnceItHasPlayed() async throws {
        let h = makeHarness()
        try await started(h, readyJSON: #"{"type":"session_state","at_ms":0,"state":"ready"}"#)
        h.transport.receive(#"{"type":"interrupted_at","at_ms":5,"offset_ms":1000}"#)
        let mp3 = Data([0x49, 0x44, 0x33, 0x04]).base64EncodedString()
        h.transport.receive(#"{"type":"audio_chunk","at_ms":6,"pcm_base64":"\#(mp3)","sample_rate":24000,"duration_ms":900,"format":"mp3"}"#)

        try await waitUntil { await h.session.snapshot().phase == .narrating && h.transport.frames("narration_resumed").count == 1 }
        let containers = await h.audio.containers
        XCTAssertEqual(containers, 1)
    }

    func testALiveChannelDyingMidSessionFallsBackToTypedQuestions() async throws {
        let h = makeHarness()
        try await started(h)
        h.transport.receive(#"{"type":"error","at_ms":9,"code":"live_unavailable","message":"channel closed"}"#)
        try await waitUntil { await !h.session.snapshot().isLive }

        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.liveUnavailable, "live_unavailable")
        XCTAssertTrue(snapshot.isRunning)
    }

    func testEndingBecauseAnotherEpisodeLoadedDoesNotPauseIt() async throws {
        let h = makeHarness()
        try await started(h)
        let before = await h.narration.pauses

        await h.session.stop(pauseNarration: false)

        let after = await h.narration.pauses
        XCTAssertEqual(after, before)
        XCTAssertTrue(h.transport.closed)
    }

    func testAMicThatDiesMidSessionEndsItAndSaysSo() async throws {
        let h = makeHarness()
        try await started(h)

        await h.audio.die("The microphone stopped: the audio route changed.")
        try await waitUntil { await h.session.snapshot().phase == .error }

        let snapshot = await h.session.snapshot()
        XCTAssertEqual(snapshot.error, "The microphone stopped: the audio route changed.")
        XCTAssertTrue(h.transport.closed)
    }

    func testANewEventTypeDoesNotTakeTheSessionDown() throws {
        XCTAssertEqual(try LiveEvent.decode(#"{"type":"something_new","at_ms":1}"#), .other(type: "something_new"))
        XCTAssertThrowsError(try LiveEvent.decode("not json"))
    }

    func testAnUnlabelledReplyIsSniffed() throws {
        let mp3 = Data([0x49, 0x44, 0x33, 0x04]).base64EncodedString()
        let event = try LiveEvent.decode(#"{"type":"audio_chunk","at_ms":1,"pcm_base64":"\#(mp3)","sample_rate":24000,"duration_ms":10}"#)
        guard case .audioChunk(_, _, _, let format) = event else { return XCTFail("\(event)") }
        XCTAssertEqual(format, "mp3")
    }
}

final class LiveAudioFormatTests: XCTestCase {
    func testMicAudioIsResampledTo16kInt16() {
        let samples = [Float](repeating: 0.5, count: 480)  // 10 ms at 48 kHz
        let pcm = LiveAudioFormat.pcm16(from: samples, sampleRate: 48_000)
        XCTAssertEqual(pcm.count, 160 * 2)
        XCTAssertEqual(LiveAudioFormat.floats(fromPCM16: pcm).first ?? 0, 0.5, accuracy: 0.001)
    }

    func testFullScaleClipsRatherThanWrapping() {
        let pcm = LiveAudioFormat.pcm16(from: [2, -2], sampleRate: 16_000)
        let values = pcm.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
        XCTAssertEqual(values, [32_767, -32_768])
    }

    func testDbfsMatchesTheServicesFloor() {
        XCTAssertEqual(LiveAudioFormat.dbfs([]), -100)
        XCTAssertEqual(LiveAudioFormat.dbfs([0, 0]), -100)
        XCTAssertEqual(LiveAudioFormat.dbfs([1, -1]), 0, accuracy: 0.001)
    }
}

extension FakeNarration {
    func setPosition(_ ms: Int) { position = ms }
}

extension LiveSessionTests {
    /// Poll with a bound: the session consumes socket events on a task of its own.
    fileprivate func waitUntil(
        _ condition: () async -> Bool, file: StaticString = #filePath, line: UInt = #line
    ) async throws {
        for _ in 0..<2_000 {
            if await condition() { return }
            try await Task.sleep(for: .milliseconds(1))
        }
        XCTFail("condition never became true", file: file, line: line)
    }
}
