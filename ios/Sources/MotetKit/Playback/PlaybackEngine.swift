import Foundation

/// What the audio layer tells the controller.
public enum PlaybackEngineEvent: Hashable, Sendable {
    /// The engine's own clock, in milliseconds from the start of the episode.
    case position(ms: Int)
    case ready(durationMs: Int)
    case playing
    case paused
    /// The audio ran to its end.
    case ended
    /// The system took the audio away — a call, Siri, another app. iOS says whether it is
    /// polite to resume when it hands it back.
    case interrupted(resumable: Bool)
    case stalled
    case failed(String)
}

/// The audio layer, behind a protocol.
///
/// `MotetPlayback`'s `AVPlayerPlaybackEngine` is the real one. The seam is not ceremony:
/// AVFoundation does not exist on Linux, does not behave the same on the simulator as on a
/// device, and cannot be driven deterministically in a test — while the rules about what a
/// skip button does, when a story counts as heard, and when a position is persisted are
/// exactly the things that must not break. Those rules live in `PlaybackController` and are
/// tested against a scripted engine.
public protocol PlaybackEngine: Sendable {
    /// Load an audio file or URL, positioned at `startingAtMs`.
    func load(url: URL, startingAtMs: Int) async throws
    func play() async
    func pause() async
    func seek(toMs: Int) async
    func setRate(_ rate: Double) async
    /// Where the engine believes it is. Advisory — the controller owns the real position.
    func currentTimeMs() async -> Int
    /// Register the sink for engine events. Called once, by the controller.
    ///
    /// The handler is `async` so that an engine which *can* deliver an event synchronously
    /// knows when the controller has finished with it. `AVPlayer`'s callbacks arrive on a
    /// dispatch queue and cannot await, so the real engine hands them to a `Task`; the
    /// scripted engine in the tests awaits, which is what makes "advance to 90s, then
    /// assert the story is marked read" a deterministic statement rather than a race.
    func setEventHandler(_ handler: @escaping @Sendable (PlaybackEngineEvent) async -> Void) async
}
