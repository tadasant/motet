import Foundation

/// The deterministic commands.
///
/// The plan calls these out separately from the voice path on purpose, and this enum is
/// where that separation is real: every case below is a pure function of the current
/// position and the settings. No model is consulted, no network call is made, nothing is
/// interpreted. A lockscreen button, a CarPlay button, a steering-wheel remote, and an
/// on-screen tap all funnel through here and do exactly what they say — including when
/// there is no signal at all.
///
/// The voice path (session `voice/`) will sit *beside* this, never underneath it: a spoken
/// "skip this one" resolves to one of these cases and then follows the same code, so an
/// interactive command can never do something a button could not.
public enum PlaybackCommand: Hashable, Sendable {
    case play
    case pause
    case togglePlayPause
    /// Jump forward by `PlaybackSettings.skipForwardMs`.
    case skipForward
    /// Jump back by `PlaybackSettings.skipBackwardMs`.
    case skipBackward
    /// To the start of the next story in the episode.
    case nextSegment
    /// To the start of this story, or the previous one if we just started this one.
    case previousSegment
    case seek(toMs: Int)
    case setRate(Double)
    /// Step up the speed ladder, wrapping.
    case cycleRate
}

/// What the UI, the lockscreen, and CarPlay all render.
public struct PlaybackSnapshot: Hashable, Sendable {
    public var episodeId: String?
    public var episodeTitle: String
    public var isPlaying: Bool
    public var positionMs: Int
    public var durationMs: Int
    public var rate: Double
    /// The story being spoken right now, for the lockscreen's subtitle.
    public var currentSegmentTitle: String?
    public var isLoading: Bool
    /// Whether this episode is playing from the device rather than the network.
    public var isOffline: Bool
    public var errorMessage: String?

    public init(
        episodeId: String? = nil,
        episodeTitle: String = "",
        isPlaying: Bool = false,
        positionMs: Int = 0,
        durationMs: Int = 0,
        rate: Double = 1.0,
        currentSegmentTitle: String? = nil,
        isLoading: Bool = false,
        isOffline: Bool = false,
        errorMessage: String? = nil
    ) {
        self.episodeId = episodeId
        self.episodeTitle = episodeTitle
        self.isPlaying = isPlaying
        self.positionMs = positionMs
        self.durationMs = durationMs
        self.rate = rate
        self.currentSegmentTitle = currentSegmentTitle
        self.isLoading = isLoading
        self.isOffline = isOffline
        self.errorMessage = errorMessage
    }

    public var hasEpisode: Bool { episodeId != nil }
}
