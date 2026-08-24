import Combine
import Foundation
import MotetKit
import SwiftUI

/// What the screens observe. A thin projection of `MotetKit` onto the main thread.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var episodes: [EpisodeResponse] = []
    @Published private(set) var newsItems: [NewsItemResponse] = []
    @Published private(set) var downloadedEpisodeIds: Set<String> = []
    @Published private(set) var positions: [String: ListeningPosition] = [:]
    @Published private(set) var playback = PlaybackSnapshot()
    @Published private(set) var isRefreshing = false
    /// Set when the last refresh could not reach the API. The library keeps serving the
    /// cache underneath it, so this is a banner rather than an error screen.
    @Published private(set) var connectionMessage: String?
    @Published var settings = PlaybackSettings()

    private let environment: AppEnvironment
    private var snapshotTask: Task<Void, Never>?

    init(environment: AppEnvironment) {
        self.environment = environment
    }

    var library: MotetLibrary { environment.library }
    var controller: PlaybackController { environment.controller }

    var isConfigured: Bool { environment.credentials.configuration().isConfigured }

    func start() async {
        await environment.activate()
        settings = (try? await library.playbackSettings()) ?? PlaybackSettings()
        observeSnapshots()
        await refresh()
    }

    private func observeSnapshots() {
        snapshotTask?.cancel()
        let controller = self.controller
        let nowPlaying = environment.nowPlaying
        snapshotTask = Task { [weak self] in
            for await snapshot in await controller.snapshots() {
                guard let self else { return }
                self.playback = snapshot
                nowPlaying.update(with: snapshot)
            }
        }
    }

    // MARK: - Loading

    func refresh() async {
        guard isConfigured else { return }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            try await library.flushPendingWrites()
            episodes = try await library.episodes()
            newsItems = try await library.newsItems()
            downloadedEpisodeIds = try await library.downloadedEpisodeIds()
            await reloadPositions()
            connectionMessage = nil
            await syncDownloads()
        } catch let error as MotetError {
            connectionMessage = error.description
            episodes = (try? await library.cachedEpisodes()) ?? episodes
        } catch {
            connectionMessage = String(describing: error)
        }
    }

    private func reloadPositions() async {
        var updated: [String: ListeningPosition] = [:]
        for episode in episodes {
            if let position = try? await library.position(forEpisode: episode.id) {
                updated[episode.id] = position
            }
        }
        positions = updated
    }

    private func syncDownloads() async {
        let pinned = playback.episodeId.map { Set([$0]) } ?? []
        _ = try? await library.syncDownloads(episodes: episodes, pinned: pinned)
        downloadedEpisodeIds = (try? await library.downloadedEpisodeIds()) ?? downloadedEpisodeIds
    }

    // MARK: - Playback

    func play(episode: EpisodeResponse) async {
        guard episode.episodeState.isPlayable else { return }
        do {
            try environment.audioSession.activate()
            let source = try await library.source(forEpisode: episode)
            try await controller.load(episode: episode, source: source, autoplay: true)
        } catch let error as MotetError {
            connectionMessage = error.description
        } catch {
            connectionMessage = String(describing: error)
        }
    }

    func perform(_ command: PlaybackCommand) async {
        await controller.perform(command)
        switch command {
        case .setRate, .cycleRate:
            // Read the rate back rather than echoing what was asked for: the controller
            // clamps to what the player reproduces without artefacts, and a UI that
            // disagrees with the player is a UI that lies.
            settings = await controller.currentSettings()
            try? await library.update(settings: settings)
        default:
            break
        }
    }

    func updateSettings(_ newSettings: PlaybackSettings) async {
        settings = newSettings
        try? await library.update(settings: newSettings)
        await controller.update(settings: newSettings)
        environment.nowPlaying.attach(to: controller, settings: newSettings)
    }

    // MARK: - Backlog

    func setRead(_ read: Bool, newsItem: NewsItemResponse) async {
        // Optimistic: the outbox is what makes this true eventually, so the row should not
        // wait for a round trip to move.
        if let index = newsItems.firstIndex(where: { $0.id == newsItem.id }) {
            newsItems[index].read = read
        }
        try? await library.setRead(read, newsItemId: newsItem.id)
    }

    func download(episode: EpisodeResponse) async {
        do {
            try await library.download(episode: episode)
            downloadedEpisodeIds = try await library.downloadedEpisodeIds()
        } catch let error as MotetError {
            connectionMessage = error.description
        } catch {
            connectionMessage = String(describing: error)
        }
    }

    func removeDownload(episode: EpisodeResponse) async {
        try? await library.removeDownload(episodeId: episode.id)
        downloadedEpisodeIds = (try? await library.downloadedEpisodeIds()) ?? downloadedEpisodeIds
    }

    func createEpisode(title: String, maxDurationMinutes: Int) async {
        do {
            _ = try await library.createEpisode(
                title: title, maxDurationMs: maxDurationMinutes * 60_000
            )
            await refresh()
        } catch let error as MotetError {
            connectionMessage = error.description
        } catch {
            connectionMessage = String(describing: error)
        }
    }

    func paste(title: String, text: String) async {
        do {
            _ = try await library.paste(title: title, text: text)
            await refresh()
        } catch let error as MotetError {
            connectionMessage = error.description
        } catch {
            connectionMessage = String(describing: error)
        }
    }

    // MARK: - Settings

    func saveCredentials(baseURL: String, apiToken: String) async {
        environment.credentials.save(baseURL: baseURL, apiToken: apiToken)
        // Rebuilds the controller *and* re-activates it, so the engine's single event
        // handler points at the new one.
        await environment.reconfigure()
        settings = (try? await library.playbackSettings()) ?? PlaybackSettings()
        observeSnapshots()
        await refresh()
    }

    func currentCredentials() -> (baseURL: String, apiToken: String) {
        let configuration = environment.credentials.configuration()
        return (configuration.baseURL?.absoluteString ?? "", configuration.apiToken ?? "")
    }

    /// Coming back to the foreground: send whatever the walk queued, and pick playback up
    /// if the system interrupted it politely.
    func handleForeground() async {
        try? await library.flushPendingWrites()
        await controller.resumeAfterInterruptionIfNeeded()
        await refresh()
    }
}
