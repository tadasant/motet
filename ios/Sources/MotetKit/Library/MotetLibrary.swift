import Foundation

/// What the app binds to: the backlog, the episodes, what is on the device, and the player.
///
/// It is offline-first in the literal sense — every read answers from the last cached
/// response when the network is not there, and every write goes to the outbox. Opening the
/// app in a basement shows the same library it showed on wifi, and the episodes that were
/// downloaded play.
public actor MotetLibrary {
    private let api: any MotetAPI
    private let cache: any KeyValueStore
    private let offline: OfflineLibrary
    private let positions: ListeningPositionStore
    private let readState: ReadStateCoordinator
    private let clock: any MotetClock

    private var settings: PlaybackSettings
    private var cachedFeedToken: String?

    private static let episodesKey = "cache.episodes"
    private static let newsItemsKey = "cache.news-items"
    private static let feedTokenKey = "cache.feed-token"
    private static let settingsKey = "settings.playback"

    public init(
        api: any MotetAPI,
        cache: any KeyValueStore,
        offline: OfflineLibrary,
        positions: ListeningPositionStore,
        readState: ReadStateCoordinator,
        clock: any MotetClock = SystemClock()
    ) {
        self.api = api
        self.cache = cache
        self.offline = offline
        self.positions = positions
        self.readState = readState
        self.clock = clock
        self.settings = (try? cache.value(PlaybackSettings.self, forKey: Self.settingsKey))
            .flatMap { $0 } ?? PlaybackSettings()
        self.cachedFeedToken = (try? cache.value(String.self, forKey: Self.feedTokenKey)).flatMap { $0 }
    }

    // MARK: - Settings

    public func playbackSettings() -> PlaybackSettings { settings }

    public func update(settings newSettings: PlaybackSettings) throws {
        settings = newSettings
        try cache.setValue(newSettings, forKey: Self.settingsKey)
    }

    // MARK: - Reads

    /// Episodes, newest first. Falls back to the cache when there is no signal.
    public func episodes(forceRefresh: Bool = true) async throws -> [EpisodeResponse] {
        if forceRefresh {
            do {
                let fresh = try await api.listEpisodes()
                try cache.setValue(fresh, forKey: Self.episodesKey)
                return fresh
            } catch let error as MotetError where error.isRetryable {
                guard let cached = try cachedEpisodes() else { throw error }
                return cached
            }
        }
        return try cachedEpisodes() ?? []
    }

    public func cachedEpisodes() throws -> [EpisodeResponse]? {
        try cache.value([EpisodeResponse].self, forKey: Self.episodesKey)
    }

    /// The backlog, with anything still queued in the outbox applied on top.
    public func newsItems(forceRefresh: Bool = true) async throws -> [NewsItemResponse] {
        var items: [NewsItemResponse]
        if forceRefresh {
            do {
                items = try await api.listNewsItems()
                try cache.setValue(items, forKey: Self.newsItemsKey)
            } catch let error as MotetError where error.isRetryable {
                guard let cached = try cache.value([NewsItemResponse].self, forKey: Self.newsItemsKey) else {
                    throw error
                }
                items = cached
            }
        } else {
            items = try cache.value([NewsItemResponse].self, forKey: Self.newsItemsKey) ?? []
        }
        return try await readState.applyPending(to: items)
    }

    public func position(forEpisode episodeId: String) async throws -> ListeningPosition? {
        try await positions.position(for: episodeId)
    }

    public func downloadedEpisodeIds() async throws -> Set<String> {
        try await offline.downloadedEpisodeIds()
    }

    public func offlineBytes() async throws -> Int {
        try await offline.totalBytes()
    }

    // MARK: - Writes

    /// Mark a news item read or unread from the backlog screen — the visual half of
    /// invariant 5, writing the same column listening does.
    public func setRead(_ read: Bool, newsItemId: String) async throws {
        try await readState.setRead(read, newsItemId: newsItemId)
    }

    public func createEpisode(
        title: String?,
        maxDurationMs: Int,
        newsItemIds: [String]? = nil,
        keepInBacklog: Bool = false
    ) async throws -> EpisodeResponse {
        try await api.createEpisode(
            title: title,
            maxDurationMs: maxDurationMs,
            newsItemIds: newsItemIds,
            keepInBacklog: keepInBacklog
        )
    }

    /// Rename an episode — the one thing about it a person chooses after it is made.
    ///
    /// Not through the outbox: renaming is a deliberate act somebody is watching the result
    /// of, and a rename replayed later from a queue would overwrite a newer one silently.
    ///
    /// **The cached list is updated with the server's answer**, because that cache is what
    /// a launch with no signal renders — without this, renaming an episode and then opening
    /// the app on a train would show the old title back again.
    public func renameEpisode(id: String, title: String) async throws -> EpisodeResponse {
        let renamed = try await api.renameEpisode(id: id, title: title)
        if let cached = try? cachedEpisodes(), let index = cached.firstIndex(where: { $0.id == id }) {
            var updated = cached
            updated[index] = renamed
            try? cache.setValue(updated, forKey: Self.episodesKey)
        }
        return renamed
    }

    /// One story with every source behind it — the provenance the backlog row links to.
    public func newsItem(id: String) async throws -> NewsItemDetailResponse {
        try await api.newsItem(id: id)
    }

    /// "Mark listened", as the SPA's shelf has it: every story read, then the position at the
    /// end — both facts, in that order, and one-way (there is no un-listening that is not
    /// un-reading). The read half goes through the outbox, so it survives no signal; the
    /// position half is best-effort, because the server's position is monotonic and a later
    /// report says the same thing.
    public func markListened(episode: EpisodeResponse) async throws {
        try await readState.markEpisodeListened(episodeId: episode.id, newsItemIds: episode.newsItemIds)
        try await positions.record(
            episodeId: episode.id,
            spokenThroughMs: episode.durationMs,
            durationMs: episode.durationMs,
            finished: true,
            coverage: ListenedCoverage(ranges: episode.durationMs > 0 ? [0..<episode.durationMs] : [])
        )
        _ = try? await api.setPlaybackPosition(
            episodeId: episode.id, listenedThroughMs: episode.durationMs
        )
    }

    public func paste(title: String, text: String) async throws -> SourceItemResponse {
        try await api.pasteSource(title: title, text: text)
    }

    /// Send anything the outbox is holding. Called on foreground and after a refresh.
    @discardableResult
    public func flushPendingWrites() async throws -> Outbox.DrainOutcome {
        try await readState.flush()
    }

    // MARK: - Offline

    /// Where to play an episode from: the device if it is there, the API if it is not.
    public func source(forEpisode episode: EpisodeResponse) async throws -> PlaybackController.Source {
        if let local = try await offline.localURL(forEpisode: episode.id) {
            return PlaybackController.Source(url: local, isLocal: true)
        }
        return PlaybackController.Source(url: try await audioURL(episodeId: episode.id), isLocal: false)
    }

    /// Download one episode by hand — the "keep this one" button.
    public func download(episode: EpisodeResponse) async throws {
        _ = try await offline.download(episodeId: episode.id, from: try await audioURL(episodeId: episode.id))
    }

    public func removeDownload(episodeId: String) async throws {
        try await offline.remove(episodeId: episodeId)
    }

    /// Bring the device's copies in line with the policy: newest ready episodes on the
    /// phone, everything else thrown away.
    @discardableResult
    public func syncDownloads(
        episodes: [EpisodeResponse], pinned: Set<String> = []
    ) async throws -> [String: Error] {
        let plan = DownloadPolicy.plan(
            episodes: episodes,
            downloaded: try await offline.downloadedEpisodeIds(),
            keep: settings.episodesToKeepOffline,
            pinned: pinned
        )
        guard !plan.isEmpty else { return [:] }
        let token = try await feedToken()
        let client = api
        return await offline.apply(plan) { episodeId in
            try client.audioURL(episodeId: episodeId, feedToken: token)
        }
    }

    private func audioURL(episodeId: String) async throws -> URL {
        try api.audioURL(episodeId: episodeId, feedToken: try await feedToken())
    }

    /// The feed token authenticates the audio route. Cached, because a download has to work
    /// when the rest of the app cannot reach the API.
    private func feedToken() async throws -> String {
        if let cachedFeedToken, !cachedFeedToken.isEmpty { return cachedFeedToken }
        let info = try await api.feedInfo()
        cachedFeedToken = info.token
        try cache.setValue(info.token, forKey: Self.feedTokenKey)
        return info.token
    }

    /// Why an episode's audio would not load, from the route — with a fresh feed token when
    /// the cached one is what was refused, so the answer is about the file and not the token.
    public func audioProblem(episodeId: String) async -> AudioProblem? {
        guard let token = try? await feedToken() else { return nil }
        let problem = await api.audioProblem(episodeId: episodeId, feedToken: token)
        guard problem == .feedTokenRefused else { return problem }
        try? invalidateFeedToken()
        guard let fresh = try? await feedToken() else { return problem }
        return await api.audioProblem(episodeId: episodeId, feedToken: fresh)
    }

    /// Drop the cached feed token — after a rotation, or a 401 on the audio route.
    public func invalidateFeedToken() throws {
        cachedFeedToken = nil
        try cache.set(nil, forKey: Self.feedTokenKey)
    }
}
