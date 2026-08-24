import Foundation

/// Where this app points and what proves it may. Supplied by the app at runtime.
///
/// Nothing here is committed. The repo is public, so no hostname, no default token, and no
/// build-time bake-in: the base URL is typed into Settings on first run and the token is
/// held in the Keychain by the app layer. A shipped default would be an infrastructure fact
/// in a public repo *and* a credential in a binary.
public struct MotetConfiguration: Hashable, Sendable {
    public var baseURL: URL?
    /// The `/v1` bearer token.
    public var apiToken: String?
    /// The feed token, which authenticates audio downloads. Fetched from `GET /v1/feed`
    /// and cached, because a download must work while the rest of the app is offline.
    public var feedToken: String?

    public init(baseURL: URL? = nil, apiToken: String? = nil, feedToken: String? = nil) {
        self.baseURL = baseURL
        self.apiToken = apiToken
        self.feedToken = feedToken
    }

    public var isConfigured: Bool {
        baseURL != nil && !(apiToken ?? "").isEmpty
    }
}

/// Listening preferences. Deterministic commands (below) read their sizes from here.
public struct PlaybackSettings: Codable, Hashable, Sendable {
    /// Playback speed. Clamped to what `AVPlayer` reproduces without artefacts.
    public var rate: Double
    /// How far `skipForward` jumps, in milliseconds.
    public var skipForwardMs: Int
    /// How far `skipBackward` jumps, in milliseconds.
    public var skipBackwardMs: Int
    /// How many of the newest ready episodes to keep on the device.
    public var episodesToKeepOffline: Int

    public static let rateRange: ClosedRange<Double> = 0.5...3.0

    public init(
        rate: Double = 1.0,
        skipForwardMs: Int = 30_000,
        skipBackwardMs: Int = 15_000,
        episodesToKeepOffline: Int = 5
    ) {
        self.rate = min(max(rate, Self.rateRange.lowerBound), Self.rateRange.upperBound)
        self.skipForwardMs = max(1_000, skipForwardMs)
        self.skipBackwardMs = max(1_000, skipBackwardMs)
        self.episodesToKeepOffline = max(0, episodesToKeepOffline)
    }

    /// The speeds the UI and the CarPlay template offer. A discrete ladder rather than a
    /// slider: choosing a speed must be possible without looking at the screen.
    public static let rateLadder: [Double] = [0.8, 1.0, 1.2, 1.5, 1.75, 2.0, 2.5, 3.0]

    /// The next speed up the ladder, wrapping — what a single "speed" button does.
    public func nextRate() -> Double {
        let ladder = Self.rateLadder
        guard let index = ladder.firstIndex(where: { $0 > rate + 0.001 }) else {
            return ladder[0]
        }
        return ladder[index]
    }
}
