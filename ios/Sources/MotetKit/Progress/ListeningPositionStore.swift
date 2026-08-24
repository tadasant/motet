import Foundation

/// Where the listener actually got to, in milliseconds.
///
/// **Invariant 4: `spoken_through_ms` is tracked by us, not the provider.** This type is
/// what "by us" means on the client. The position is written here, durably, from the
/// controller's own accounting of playback; it is never read back out of `AVPlayer` as the
/// source of truth. A player reports 0 while it re-buffers after an interruption, reports
/// the *item's* time rather than the episode's when the queue is rebuilt, and forgets
/// everything when the process is killed. Any one of those, trusted, silently rewinds a
/// walk.
public struct ListeningPosition: Codable, Hashable, Sendable {
    public let episodeId: String
    /// Where playback resumes. Moves backwards when the listener seeks back.
    public var spokenThroughMs: Int
    /// The furthest point ever reached. Only ever increases.
    ///
    /// Read state keys off *this*, not off `spokenThroughMs`: hearing a story and then
    /// scrubbing back to re-hear the start of the episode must not mark it unread.
    public var furthestSpokenMs: Int
    public var durationMs: Int
    public var updatedAt: Date
    /// Whether the episode ran to its end at least once.
    public var isFinished: Bool
    /// The parts of the audio that were actually played, which is what read state is
    /// computed from. Decoded leniently: a position stored before this field existed is
    /// still readable, and falls back to "everything up to `furthestSpokenMs`".
    public var heardRanges: [Range<Int>]

    public init(
        episodeId: String,
        spokenThroughMs: Int,
        furthestSpokenMs: Int,
        durationMs: Int,
        updatedAt: Date,
        isFinished: Bool = false,
        heardRanges: [Range<Int>] = []
    ) {
        self.episodeId = episodeId
        self.spokenThroughMs = spokenThroughMs
        self.furthestSpokenMs = furthestSpokenMs
        self.durationMs = durationMs
        self.updatedAt = updatedAt
        self.isFinished = isFinished
        self.heardRanges = heardRanges
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        episodeId = try container.decode(String.self, forKey: .episodeId)
        spokenThroughMs = try container.decode(Int.self, forKey: .spokenThroughMs)
        furthestSpokenMs = try container.decode(Int.self, forKey: .furthestSpokenMs)
        durationMs = try container.decode(Int.self, forKey: .durationMs)
        updatedAt = try container.decode(Date.self, forKey: .updatedAt)
        isFinished = try container.decodeIfPresent(Bool.self, forKey: .isFinished) ?? false
        heardRanges = try container.decodeIfPresent([Range<Int>].self, forKey: .heardRanges) ?? []
    }

    /// What was heard, with the pre-coverage fallback applied.
    public var coverage: ListenedCoverage {
        heardRanges.isEmpty
            ? ListenedCoverage(ranges: furthestSpokenMs > 0 ? [0..<furthestSpokenMs] : [])
            : ListenedCoverage(ranges: heardRanges)
    }

    /// 0...1, for a progress bar.
    public var fraction: Double {
        guard durationMs > 0 else { return 0 }
        return min(1.0, max(0.0, Double(spokenThroughMs) / Double(durationMs)))
    }
}

/// Durable listening positions, one per episode.
///
/// Writes are throttled by the caller, not here: this store is happy to be written on every
/// tick, and the controller decides how often that is worth the flash write.
public actor ListeningPositionStore {
    private let store: any KeyValueStore
    private let clock: any MotetClock
    private var cache: [String: ListeningPosition] = [:]
    private var loaded = false

    private static let prefix = "position."

    public init(store: any KeyValueStore, clock: any MotetClock = SystemClock()) {
        self.store = store
        self.clock = clock
    }

    private func loadIfNeeded() throws {
        guard !loaded else { return }
        for key in try store.keys(withPrefix: Self.prefix) {
            if let position = try store.value(ListeningPosition.self, forKey: key) {
                cache[position.episodeId] = position
            }
        }
        loaded = true
    }

    public func position(for episodeId: String) throws -> ListeningPosition? {
        try loadIfNeeded()
        return cache[episodeId]
    }

    public func all() throws -> [ListeningPosition] {
        try loadIfNeeded()
        return cache.values.sorted { $0.updatedAt > $1.updatedAt }
    }

    /// Record a position. Returns the stored result, with `furthestSpokenMs` carried over.
    @discardableResult
    public func record(
        episodeId: String,
        spokenThroughMs: Int,
        durationMs: Int,
        finished: Bool = false,
        coverage: ListenedCoverage? = nil
    ) throws -> ListeningPosition {
        try loadIfNeeded()
        let clamped = max(0, durationMs > 0 ? min(spokenThroughMs, durationMs) : spokenThroughMs)
        let existing = cache[episodeId]
        let position = ListeningPosition(
            episodeId: episodeId,
            spokenThroughMs: clamped,
            furthestSpokenMs: max(clamped, existing?.furthestSpokenMs ?? 0),
            durationMs: durationMs,
            updatedAt: clock.now,
            isFinished: finished || (existing?.isFinished ?? false),
            heardRanges: coverage?.ranges ?? existing?.heardRanges ?? []
        )
        cache[episodeId] = position
        try store.setValue(position, forKey: Self.prefix + episodeId)
        return position
    }

    public func forget(episodeId: String) throws {
        try loadIfNeeded()
        cache[episodeId] = nil
        try store.set(nil, forKey: Self.prefix + episodeId)
    }
}
