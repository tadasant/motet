import Foundation

/// Where an episode is in the factory. The wire carries a string; the app switches on this.
///
/// `unknown` exists because the app ships through App Store review and the API does not: a
/// state added server-side must render as "something is happening" on an installed build,
/// never as a crash.
public enum EpisodeState: Hashable, Sendable {
    case pending
    case scripting
    case rendering
    case ready
    case failed
    case unknown(String)

    public init(wire: String) {
        switch wire {
        case "pending": self = .pending
        case "scripting": self = .scripting
        case "rendering": self = .rendering
        case "ready": self = .ready
        case "failed": self = .failed
        default: self = .unknown(wire)
        }
    }

    /// Whether audio exists to play or download.
    public var isPlayable: Bool { self == .ready }

    /// Whether the app should keep polling this episode.
    public var isInProgress: Bool {
        switch self {
        case .pending, .scripting, .rendering: return true
        case .ready, .failed, .unknown: return false
        }
    }

    public var displayName: String {
        switch self {
        case .pending: return "Queued"
        case .scripting: return "Writing"
        case .rendering: return "Recording"
        case .ready: return "Ready"
        case .failed: return "Failed"
        case .unknown(let raw): return raw.capitalized
        }
    }
}

extension EpisodeResponse {
    public var episodeState: EpisodeState { EpisodeState(wire: state) }

    /// Every news item this episode speaks, in the order it speaks them.
    public var newsItemIds: [String] {
        var seen = Set<String>()
        return segments.compactMap { segment in
            seen.insert(segment.newsItemId).inserted ? segment.newsItemId : nil
        }
    }

    public var timeline: SegmentTimeline {
        SegmentTimeline(segments: segments, episodeDurationMs: durationMs)
    }
}
