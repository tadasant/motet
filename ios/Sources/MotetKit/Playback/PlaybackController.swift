import Foundation

/// The player's brain: it owns the position, translates commands, and keeps read state
/// honest. Everything here is deterministic and testable; nothing here talks to AVFoundation.
///
/// Three responsibilities, all of them invariants rather than features:
///
/// * **Position is ours** (invariant 4). The engine's clock is an input, not the truth. The
///   controller carries the position across interruptions, seeks, and process death, and
///   writes it durably.
/// * **Read state is per News Item** (invariant 5). Crossing the end of a story's last
///   segment is the same fact as tapping `read` on the backlog, and it goes through the
///   same API call, queued if there is no signal.
/// * **Commands are deterministic**. `PlaybackCommand` is a closed set of pure state
///   transitions, shared by the UI, the lockscreen, the remote, and CarPlay.
public actor PlaybackController {
    /// Where the audio comes from — a file on the device, or the API.
    public struct Source: Hashable, Sendable {
        public let url: URL
        public let isLocal: Bool

        public init(url: URL, isLocal: Bool) {
            self.url = url
            self.isLocal = isLocal
        }
    }

    private let engine: any PlaybackEngine
    private let positions: ListeningPositionStore
    private let readState: ReadStateCoordinator
    private let clock: any MotetClock

    private var settings: PlaybackSettings
    private var episode: EpisodeResponse?
    private var timeline = SegmentTimeline(entries: [], episodeDurationMs: 0)
    private var positionMs = 0
    private var furthestMs = 0
    /// What was actually played, as opposed to where the playhead has been. Read state is
    /// computed from this — see `ListenedCoverage`.
    private var coverage = ListenedCoverage()
    private var isPlaying = false
    private var isLoading = false
    private var isLocal = false
    private var errorMessage: String?
    private var markedHeard: Set<String> = []
    private var didMarkListened = false
    private var lastPersistAt: Date?
    private var lastPersistMs = 0
    private var shouldResumeAfterInterruption = false
    /// Where the server's `listened_through_ms` is for the loaded episode, as far as this
    /// device knows: the value the episode arrived with, moved by every report it accepted.
    private var serverFrontierMs = 0
    private var lastFailedReportAt: Date?
    private var isReporting = false
    private let reportPosition: PositionReporter?
    private var observers: [UUID: AsyncStream<PlaybackSnapshot>.Continuation] = [:]

    /// How often a position is written to flash while playing. Every tick would be a write
    /// per second for the length of a walk; a lost 3 seconds is not worth that.
    static let persistIntervalSeconds: TimeInterval = 3

    /// The largest forward jump between two position reports that still counts as *listening*.
    ///
    /// This is what separates "the audio played" from "the listener moved the playhead", and
    /// read state depends on the distinction: a skip that landed past the end of a story must
    /// not mark that story read (invariant 5 — the backlog is the product's memory). The
    /// engine reports about once a second, so even at 3× the real step is ~3s; anything
    /// larger is a seek, a resume from a stall, or a jump the app itself asked for.
    static let maxListeningStepMs = 5_000

    /// Writes the listening frontier to the server — `PUT /v1/episodes/{id}/position` — and
    /// answers with the value the server holds afterwards. The server's value is what makes
    /// a position cross-device: a phone that never played an episode resumes where a laptop
    /// got to, and the other way round (motet#11). Nil in tests that do not care.
    public typealias PositionReporter = @Sendable (_ episodeId: String, _ listenedThroughMs: Int) async throws -> Int

    /// How much new listening is worth a report while playing. The SPA reports every ten
    /// seconds of playback; a pause, a finish and an unload report whatever there is.
    static let reportStepMs = 10_000
    /// How long to leave the server alone after a report that did not arrive, so a walk with
    /// no signal is not a request per position tick. The next report carries the same
    /// frontier, so nothing is lost by waiting.
    static let reportRetrySeconds: TimeInterval = 10

    public init(
        engine: any PlaybackEngine,
        positions: ListeningPositionStore,
        readState: ReadStateCoordinator,
        settings: PlaybackSettings = PlaybackSettings(),
        clock: any MotetClock = SystemClock(),
        reportPosition: PositionReporter? = nil
    ) {
        self.engine = engine
        self.positions = positions
        self.readState = readState
        self.settings = settings
        self.clock = clock
        self.reportPosition = reportPosition
    }

    /// Subscribe the controller to its engine. Call once, at startup.
    public func activate() async {
        await engine.setEventHandler { [weak self] event in
            await self?.handle(event)
        }
    }

    // MARK: - State out

    public func snapshot() -> PlaybackSnapshot {
        PlaybackSnapshot(
            episodeId: episode?.id,
            episodeTitle: episode?.title ?? "",
            isPlaying: isPlaying,
            positionMs: positionMs,
            durationMs: episode?.durationMs ?? timeline.episodeDurationMs,
            rate: settings.rate,
            currentSegmentTitle: timeline.entry(at: positionMs)?.newsItemTitle,
            isLoading: isLoading,
            isOffline: isLocal,
            errorMessage: errorMessage
        )
    }

    /// A stream of snapshots, for the UI, the lockscreen, and CarPlay to render.
    public func snapshots() -> AsyncStream<PlaybackSnapshot> {
        AsyncStream { continuation in
            let id = UUID()
            observers[id] = continuation
            continuation.yield(snapshot())
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeObserver(id) }
            }
        }
    }

    private func removeObserver(_ id: UUID) {
        observers[id] = nil
    }

    private func publish() {
        let current = snapshot()
        for continuation in observers.values {
            continuation.yield(current)
        }
    }

    public func currentSettings() -> PlaybackSettings { settings }

    /// The news item being spoken right now, for the episode screen's "you are here".
    public func currentNewsItemId() -> String? {
        timeline.entry(at: positionMs)?.newsItemId
    }

    public func update(settings newSettings: PlaybackSettings) async {
        let rateChanged = newSettings.rate != settings.rate
        settings = newSettings
        if rateChanged {
            await engine.setRate(newSettings.rate)
        }
        publish()
    }

    // MARK: - Loading

    /// Put an episode in the player, positioned where the listener left it.
    public func load(episode newEpisode: EpisodeResponse, source: Source, autoplay: Bool) async throws {
        if episode?.id != newEpisode.id {
            await persistPosition(force: true)
            await reportFrontier(force: true)
            markedHeard.removeAll()
            didMarkListened = false
            serverFrontierMs = 0
            lastFailedReportAt = nil
            await readState.resetPlaybackDedup()
        }

        episode = newEpisode
        timeline = newEpisode.timeline
        isLocal = source.isLocal
        errorMessage = nil
        isLoading = true
        publish()

        let stored = try? await positions.position(for: newEpisode.id)
        coverage = stored?.coverage ?? ListenedCoverage()
        // Resuming at the very end would immediately re-fire "ended"; a finished episode
        // starts again from the top, which is what a listener expects from one they
        // already heard.
        let localResume: Int = {
            guard let stored, !stored.isFinished else { return 0 }
            return stored.spokenThroughMs >= newEpisode.durationMs - 1_000 ? 0 : stored.spokenThroughMs
        }()
        // The server's position is the furthest anyone listened, on any device. Where it is
        // past everything *this* device ever heard, the listening happened elsewhere and it
        // wins; where it is not, the device's own playhead stands — including one the
        // listener deliberately scrubbed back to, which is device-local on purpose.
        let server = newEpisode.listenedThroughMs
        serverFrontierMs = max(serverFrontierMs, server)
        let heardHere = max(stored?.furthestSpokenMs ?? 0, stored?.coverage.upperBound ?? 0)
        let resumeAt = server > heardHere && server < newEpisode.durationMs - 1_000
            ? server : localResume
        positionMs = resumeAt
        furthestMs = max(resumeAt, stored?.furthestSpokenMs ?? 0)
        // Anything already heard in a previous session must not be re-marked.
        markedHeard = Set(timeline.newsItemsCompleted(coverage: coverage))

        do {
            try await engine.load(url: source.url, startingAtMs: resumeAt)
            await engine.setRate(settings.rate)
        } catch {
            isLoading = false
            errorMessage = String(describing: error)
            publish()
            throw error
        }

        isLoading = false
        publish()
        if autoplay {
            await perform(.play)
        }
    }

    /// Take the episode out of the player, persisting where we got to.
    public func unload() async {
        await persistPosition(force: true)
        await reportFrontier(force: true)
        await engine.pause()
        episode = nil
        timeline = SegmentTimeline(entries: [], episodeDurationMs: 0)
        positionMs = 0
        furthestMs = 0
        isPlaying = false
        publish()
    }

    // MARK: - Commands

    /// Every button, remote command, and CarPlay tap comes through here.
    public func perform(_ command: PlaybackCommand) async {
        guard episode != nil else { return }
        switch command {
        case .play:
            await engine.play()
            isPlaying = true
        case .pause:
            await engine.pause()
            isPlaying = false
            await persistPosition(force: true)
            await reportFrontier(force: true)
        case .togglePlayPause:
            await perform(isPlaying ? .pause : .play)
            return
        case .skipForward:
            await seek(to: positionMs + settings.skipForwardMs)
        case .skipBackward:
            await seek(to: positionMs - settings.skipBackwardMs)
        case .nextSegment:
            await seek(to: timeline.startOfNextEntry(from: positionMs) ?? duration)
        case .previousSegment:
            await seek(to: timeline.startOfPreviousEntry(from: positionMs) ?? 0)
        case .seek(let target):
            await seek(to: target)
        case .setRate(let rate):
            var updated = settings
            updated.rate = min(max(rate, PlaybackSettings.rateRange.lowerBound),
                               PlaybackSettings.rateRange.upperBound)
            await update(settings: updated)
            return
        case .cycleRate:
            var updated = settings
            updated.rate = settings.nextRate()
            await update(settings: updated)
            return
        }
        publish()
    }

    private var duration: Int { episode?.durationMs ?? timeline.episodeDurationMs }

    private func seek(to target: Int) async {
        let clamped = max(0, min(target, duration))
        positionMs = clamped
        // Deliberately does NOT move `furthestMs`: jumping over a story is not hearing it.
        // The engine echoes a `.position` for the seek, which `observed(positionMs:)` then
        // sees as a discontinuity and refuses to count.
        await engine.seek(toMs: clamped)
        await persistPosition(force: true)
        publish()
    }

    // MARK: - Engine events

    func handle(_ event: PlaybackEngineEvent) async {
        switch event {
        case .position(let ms):
            await observed(positionMs: ms)
        case .ready(let durationMs):
            if durationMs > 0, timeline.episodeDurationMs == 0 {
                timeline = SegmentTimeline(entries: timeline.entries, episodeDurationMs: durationMs)
            }
            isLoading = false
        case .playing:
            isPlaying = true
            errorMessage = nil
        case .paused:
            isPlaying = false
            await persistPosition(force: true)
            await reportFrontier(force: true)
        case .ended:
            await finish()
        case .interrupted(let resumable):
            // The system took the audio. Keep our own position — the engine's will be
            // wrong or zero when it comes back (invariant 4).
            shouldResumeAfterInterruption = resumable && isPlaying
            isPlaying = false
            await persistPosition(force: true)
        case .stalled:
            isLoading = true
        case .failed(let message):
            errorMessage = message
            isPlaying = false
            isLoading = false
            await persistPosition(force: true)
        }
        publish()
    }

    /// Resume after an interruption that said it was polite to. Called by the app layer.
    public func resumeAfterInterruptionIfNeeded() async {
        guard shouldResumeAfterInterruption else { return }
        shouldResumeAfterInterruption = false
        await perform(.play)
    }

    private func observed(positionMs ms: Int) async {
        let previous = positionMs
        positionMs = max(0, ms)

        // Only continuous forward progress while playing counts as having been *heard*.
        // A seek's echo, a jump the remote asked for, or a stale report arriving out of
        // order all fail this test and move the playhead without moving read state.
        let step = positionMs - previous
        if isPlaying, step > 0, step <= Self.maxListeningStepMs {
            coverage.add(from: previous, to: positionMs)
            furthestMs = max(furthestMs, positionMs)
        }

        await markNewlyHeard()
        await persistPosition(force: false)
        await reportFrontier(force: false)
    }

    /// Every story whose segments were actually played is read (invariant 5).
    private func markNewlyHeard() async {
        let completed = timeline.newsItemsCompleted(coverage: coverage)
        let fresh = completed.filter { !markedHeard.contains($0) }
        guard !fresh.isEmpty else { return }
        markedHeard.formUnion(fresh)
        try? await readState.markHeard(newsItemIds: fresh)
        // Write the coverage that justified this immediately: if the app dies before the
        // next throttled write, the next launch would recompute "not heard yet" and report
        // the same items again.
        await persistPosition(force: true)
    }

    private func finish() async {
        guard let episode, !didMarkListened else { return }
        didMarkListened = true
        // The file ran out, so whatever *was* playing was heard to its end — but only from
        // wherever the listener last landed. Scrubbing to the last ten seconds of a
        // half-hour episode and letting it run out is not hearing the episode.
        if isPlaying, duration - furthestMs <= Self.maxListeningStepMs {
            coverage.add(from: furthestMs, to: duration)
            furthestMs = duration
        }
        positionMs = duration
        isPlaying = false
        await markNewlyHeard()

        try? await positions.record(
            episodeId: episode.id,
            spokenThroughMs: duration,
            durationMs: duration,
            finished: true,
            coverage: coverage
        )

        // `POST /v1/episodes/{id}/listened` marks *every* item in the episode read in one
        // server-side write, so it is only honest when every item really was heard. It
        // still earns its place: it closes any item whose boundary no position tick landed
        // inside, which the per-item writes above cannot.
        await reportFrontier(force: true)

        let heardEverything = episode.newsItemIds.allSatisfy { markedHeard.contains($0) }
        if heardEverything {
            try? await readState.markEpisodeListened(
                episodeId: episode.id, newsItemIds: episode.newsItemIds
            )
        }
    }

    /// Move the server's position to the end of the listening that is unbroken from where
    /// the server already is (`ListenedCoverage.frontier`) — never to the playhead, which a
    /// seek moves, and never past a story that was skipped.
    ///
    /// Best-effort and not queued: the position is monotonic on the server and every later
    /// report carries the same frontier or a further one, so a report lost to no signal is
    /// made good by the next one rather than by an outbox.
    private func reportFrontier(force: Bool) async {
        guard let episode, let reportPosition, !isReporting else { return }
        let frontier = min(coverage.frontier(from: serverFrontierMs), duration)
        guard frontier > serverFrontierMs else { return }
        let now = clock.now
        if !force {
            guard frontier - serverFrontierMs >= Self.reportStepMs else { return }
            if let last = lastFailedReportAt, now.timeIntervalSince(last) < Self.reportRetrySeconds {
                return
            }
        }
        isReporting = true
        let episodeId = episode.id
        let accepted = try? await reportPosition(episodeId, frontier)
        isReporting = false
        lastFailedReportAt = accepted == nil ? now : nil
        // The episode may have changed while the request was out; its answer is about the
        // one it was sent for.
        if let accepted, self.episode?.id == episodeId {
            serverFrontierMs = max(serverFrontierMs, accepted)
        }
    }

    private func persistPosition(force: Bool) async {
        guard let episode else { return }
        let now = clock.now
        if !force, let last = lastPersistAt,
           now.timeIntervalSince(last) < Self.persistIntervalSeconds,
           abs(positionMs - lastPersistMs) < Int(Self.persistIntervalSeconds * 1_000) {
            return
        }
        lastPersistAt = now
        lastPersistMs = positionMs
        try? await positions.record(
            episodeId: episode.id,
            spokenThroughMs: positionMs,
            durationMs: duration,
            finished: false,
            coverage: coverage
        )
    }
}
