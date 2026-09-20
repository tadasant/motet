import Foundation

/// What the audio layer can be asked about itself, on demand.
///
/// Separate from ``PlaybackEngine`` rather than bolted onto it, and that is deliberate:
/// `PlaybackEngine` is the command surface the controller drives, every fake in the tests
/// implements it, and widening it would make every one of them answer a question about
/// hardware it does not have. This is asked of the *real* engine by the diagnostics layer
/// and by nothing else.
public protocol PlaybackEngineProbe: Sendable {
    func engineFacts() async -> EngineFacts
}

/// What the audio *session* can be asked. Same argument, one layer down: the session is
/// process-wide state the player does not own.
public protocol AudioRouteProbe: Sendable {
    func routeFacts() -> AudioRoute?
}

/// The engine's own account of itself, read live rather than reconstructed from the event
/// stream — `reasonForWaitingToPlay` is only readable while the wait is on, and a level is
/// only a number while something is measuring it.
public struct EngineFacts: Hashable, Sendable {
    public var transport: PlaybackTransport
    public var positionMs: Int
    public var rate: Double
    public var waitReason: PlaybackWaitReason?
    public var level: AudioLevel?
    public var errorMessage: String?

    public init(
        transport: PlaybackTransport = .paused,
        positionMs: Int = 0,
        rate: Double = 0,
        waitReason: PlaybackWaitReason? = nil,
        level: AudioLevel? = nil,
        errorMessage: String? = nil
    ) {
        self.transport = transport
        self.positionMs = positionMs
        self.rate = rate
        self.waitReason = waitReason
        self.level = level
        self.errorMessage = errorMessage
    }
}

/// Folds ``EngineFacts`` and ``AudioRoute`` through ``PlaybackClockWatch`` into a
/// ``PlaybackProbe``, and remembers enough to answer "how long has it been like this".
///
/// An actor because the app polls it from the main actor while the engine answers from
/// wherever AVFoundation's callbacks landed, and because the watch is mutable state that
/// two samples must not interleave on.
public actor PlaybackProbeRecorder {
    private let engine: any PlaybackEngineProbe
    private let route: (any AudioRouteProbe)?
    private let clock: any MotetClock
    private var watch = PlaybackClockWatch()
    private var latest = PlaybackProbe()
    private var episodeId: String?

    public init(
        engine: any PlaybackEngineProbe,
        route: (any AudioRouteProbe)? = nil,
        clock: any MotetClock = SystemClock()
    ) {
        self.engine = engine
        self.route = route
        self.clock = clock
    }

    /// The episode the probe's lines are about. Setting a different one resets the window:
    /// the previous episode's positions are not this one's.
    public func track(episodeId: String?) {
        guard self.episodeId != episodeId else { return }
        self.episodeId = episodeId
        watch.reset()
    }

    /// Forget the window — after a seek, a load, or a resume, none of which is listening.
    public func discontinuity() {
        watch.reset()
    }

    /// Take one sample and answer the probe it produces.
    @discardableResult
    public func sample() async -> PlaybackProbe {
        let facts = await engine.engineFacts()
        let now = clock.now
        // A player that is not running has no clock to watch, and leaving stale samples in
        // the window would make the first tick after a resume look like a jump.
        if facts.transport == .playing {
            watch.observe(positionMs: facts.positionMs, at: now)
        } else {
            watch.reset()
        }
        let probe = PlaybackProbe(
            transport: facts.transport,
            positionMs: facts.positionMs,
            advancedMs: watch.advancedMs,
            rate: facts.rate,
            waitReason: facts.waitReason,
            level: facts.level,
            route: route?.routeFacts(),
            errorMessage: facts.errorMessage,
            episodeId: episodeId
        )
        latest = probe
        return probe
    }

    public func current() -> PlaybackProbe { latest }

    /// Whether the window is wide enough for ``PlaybackProbe/advancedMs`` to be evidence.
    /// A caller that raises an alarm on a frozen clock must wait for this; a caller that
    /// only displays the number need not.
    public func isConclusive() -> Bool { watch.isConclusive }
}
