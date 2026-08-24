import Foundation

/// Read state, as one fact reachable two ways.
///
/// Invariant 5: read state is per News Item, and syncs across audio and visual. Tapping
/// "read" on the web backlog and listening past a story in an episode write the same
/// column, so this type never keeps a private notion of "listened" — it queues the same
/// two API calls the SPA makes and overlays what has not drained yet.
public actor ReadStateCoordinator {
    private let outbox: Outbox
    private let api: any MotetAPI
    /// News items already marked read from playback this run, so a position tick that
    /// crosses the same boundary twice does not enqueue twice.
    private var markedFromPlayback: Set<String> = []

    public init(api: any MotetAPI, outbox: Outbox) {
        self.api = api
        self.outbox = outbox
    }

    /// A deliberate tap in the UI.
    public func setRead(_ read: Bool, newsItemId: String) async throws {
        if read { markedFromPlayback.insert(newsItemId) } else { markedFromPlayback.remove(newsItemId) }
        try await outbox.enqueue(.newsItemRead(newsItemId: newsItemId, read: read))
        _ = try? await outbox.drain(using: api)
    }

    /// The listener heard these all the way through.
    public func markHeard(newsItemIds: [String]) async throws {
        let fresh = newsItemIds.filter { !markedFromPlayback.contains($0) }
        guard !fresh.isEmpty else { return }
        for id in fresh {
            markedFromPlayback.insert(id)
            try await outbox.enqueue(.newsItemRead(newsItemId: id, read: true))
        }
        _ = try? await outbox.drain(using: api)
    }

    /// The episode reached its end.
    ///
    /// `POST /v1/episodes/{id}/listened` marks every news item in the episode read in one
    /// server-side write — the same write the RSS-era "mark listened" button made. Sending
    /// it in addition to the per-item writes is not redundant: it closes any item whose
    /// segment boundary the client never observed a tick inside.
    public func markEpisodeListened(episodeId: String, newsItemIds: [String]) async throws {
        markedFromPlayback.formUnion(newsItemIds)
        try await outbox.enqueue(.episodeListened(episodeId: episodeId))
        _ = try? await outbox.drain(using: api)
    }

    /// Apply anything still queued on top of a server response.
    public func applyPending(to items: [NewsItemResponse]) async throws -> [NewsItemResponse] {
        var result: [NewsItemResponse] = []
        result.reserveCapacity(items.count)
        for var item in items {
            if let pending = try await outbox.pendingReadState(forNewsItem: item.id) {
                item.read = pending
            }
            result.append(item)
        }
        return result
    }

    /// Try to send whatever is queued. Called on foreground, on reconnect, and after a
    /// refresh.
    @discardableResult
    public func flush() async throws -> Outbox.DrainOutcome {
        try await outbox.drain(using: api)
    }

    /// Forget the per-run dedup set — used when the player moves to a different episode.
    public func resetPlaybackDedup() {
        markedFromPlayback.removeAll()
    }
}
