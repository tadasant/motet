import Foundation

/// A write the app owes the server.
///
/// Read state is per News Item and syncs across audio and visual (invariant 5) — which
/// means a story heard on a dog walk with no signal has to reach the same column the web
/// backlog writes, later. Without a durable queue that fact lives only in the player's
/// memory and dies with the process, and the app becomes the "local-only copy" the design
/// says it must not be.
public struct OutboxEntry: Codable, Hashable, Sendable, Identifiable {
    public enum Kind: Codable, Hashable, Sendable {
        /// `POST /v1/news-items/{id}/read`
        case newsItemRead(newsItemId: String, read: Bool)
        /// `POST /v1/episodes/{id}/listened`
        case episodeListened(episodeId: String)
    }

    public let id: UUID
    public let kind: Kind
    public let createdAt: Date
    public var attempts: Int
    /// The earliest this may be retried. Set by the backoff after a failure.
    public var notBefore: Date

    public init(id: UUID, kind: Kind, createdAt: Date, attempts: Int = 0, notBefore: Date) {
        self.id = id
        self.kind = kind
        self.createdAt = createdAt
        self.attempts = attempts
        self.notBefore = notBefore
    }

    /// Two entries with the same coalescing key describe the same fact, so only the last
    /// one matters: marking an item read, then unread, then read again should send one
    /// write, not three.
    public var coalescingKey: String {
        switch kind {
        case .newsItemRead(let newsItemId, _): return "news-item:\(newsItemId)"
        case .episodeListened(let episodeId): return "episode-listened:\(episodeId)"
        }
    }
}

/// The durable queue of pending writes, drained in order whenever the network is back.
///
/// Ordering matters and is FIFO: read-then-unread must not arrive as unread-then-read. A
/// single failing entry therefore stops the drain rather than being skipped — except when
/// the failure is permanent (a 4xx that is not a timeout), where the entry is dropped,
/// because a request the server will never accept would otherwise wedge every write behind
/// it forever.
public actor Outbox {
    private let store: any KeyValueStore
    private let clock: any MotetClock
    private var entries: [OutboxEntry] = []
    private var loaded = false
    private var draining = false

    private static let key = "outbox.pending"
    /// Retry backoff, in seconds, by attempt count. Capped rather than unbounded: the app
    /// is usually offline for the length of a walk, not a week.
    static let backoffSeconds: [TimeInterval] = [0, 2, 10, 30, 120, 300]

    public init(store: any KeyValueStore, clock: any MotetClock = SystemClock()) {
        self.store = store
        self.clock = clock
    }

    private func loadIfNeeded() throws {
        guard !loaded else { return }
        entries = (try? store.value([OutboxEntry].self, forKey: Self.key)) as? [OutboxEntry] ?? []
        loaded = true
    }

    private func persist() throws {
        try store.setValue(entries, forKey: Self.key)
    }

    public func pending() throws -> [OutboxEntry] {
        try loadIfNeeded()
        return entries
    }

    /// Queue a write, replacing any earlier entry describing the same fact.
    public func enqueue(_ kind: OutboxEntry.Kind, id: UUID = UUID()) throws {
        try loadIfNeeded()
        let entry = OutboxEntry(id: id, kind: kind, createdAt: clock.now, notBefore: clock.now)
        entries.removeAll { $0.coalescingKey == entry.coalescingKey }
        entries.append(entry)
        try persist()
    }

    /// The most recent queued read state for a news item, if any.
    ///
    /// The backlog screen reads this so a pending change survives a refresh: a server list
    /// fetched before the queue drains still says `read: false`, and flipping the row back
    /// under the listener's thumb is how an app teaches someone not to trust it.
    public func pendingReadState(forNewsItem newsItemId: String) throws -> Bool? {
        try loadIfNeeded()
        for entry in entries.reversed() {
            if case .newsItemRead(let id, let read) = entry.kind, id == newsItemId {
                return read
            }
        }
        return nil
    }

    public enum DrainOutcome: Hashable, Sendable {
        /// Everything that was due went through.
        case drained(count: Int)
        /// Stopped on a retryable failure; the queue is intact and will be retried.
        case deferred(remaining: Int)
        /// Nothing was due.
        case idle
    }

    /// Send what is due. Safe to call often — a second call while one is running is a no-op.
    @discardableResult
    public func drain(using api: any MotetAPI) async throws -> DrainOutcome {
        try loadIfNeeded()
        guard !draining else { return .idle }
        draining = true
        defer { draining = false }

        var sent = 0
        while let entry = entries.first {
            guard entry.notBefore <= clock.now else {
                return sent > 0 ? .drained(count: sent) : .deferred(remaining: entries.count)
            }
            do {
                try await send(entry, using: api)
                entries.removeFirst()
                try persist()
                sent += 1
            } catch let error as MotetError where error.isRetryable {
                var retried = entries.removeFirst()
                retried.attempts += 1
                let index = min(retried.attempts, Self.backoffSeconds.count - 1)
                retried.notBefore = clock.now.addingTimeInterval(Self.backoffSeconds[index])
                entries.insert(retried, at: 0)
                try persist()
                return sent > 0 ? .drained(count: sent) : .deferred(remaining: entries.count)
            } catch {
                // Permanent: the server will never accept this request. Drop it rather than
                // block every later write behind it.
                entries.removeFirst()
                try persist()
            }
        }
        return sent > 0 ? .drained(count: sent) : .idle
    }

    private func send(_ entry: OutboxEntry, using api: any MotetAPI) async throws {
        switch entry.kind {
        case .newsItemRead(let newsItemId, let read):
            _ = try await api.setNewsItemRead(id: newsItemId, read: read)
        case .episodeListened(let episodeId):
            _ = try await api.markEpisodeListened(id: episodeId)
        }
    }
}
