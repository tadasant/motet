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
    /// The Google account the app is signed in as, or nil for a pasted token or none.
    @Published private(set) var signedInEmail: String?
    @Published private(set) var isSigningIn = false
    /// Why the last sign-in did not finish. Shown under the button, never as a modal.
    @Published private(set) var signInMessage: String?

    private let environment: AppEnvironment
    private var snapshotTask: Task<Void, Never>?

    init(environment: AppEnvironment) {
        self.environment = environment
        self.signedInEmail = environment.credentials.signedInEmail
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
        let previous = environment.credentials.configuration()
        let token = apiToken.trimmingCharacters(in: .whitespacesAndNewlines)
        if signedInEmail != nil, let old = previous.apiToken, !old.isEmpty, old != token {
            // Replacing a signed-in session: revoke it on the server rather than leave it
            // valid for the rest of its thirty days on a phone that no longer uses it.
            try? await MotetHTTPClient(configuration: previous).signOut()
        }
        environment.credentials.save(baseURL: baseURL, apiToken: apiToken)
        signedInEmail = environment.credentials.signedInEmail
        await applyCredentialChange()
    }

    private func applyCredentialChange() async {
        // Rebuilds the controller *and* re-activates it, so the engine's single event
        // handler points at the new one.
        await environment.reconfigure()
        settings = (try? await library.playbackSettings()) ?? PlaybackSettings()
        observeSnapshots()
        await refresh()
    }

    // MARK: - Signing in (AGENTS.md, "The phone signs in through the web sign-in")

    /// Ask the server on screen — saved or only typed — to start a Google sign-in for this
    /// app. The caller opens `url` in the system sign-in sheet and hands whatever comes back
    /// to `finishSignIn`, with the same server.
    func beginSignIn(baseURL: String) async -> StartedSignIn? {
        signInMessage = nil
        guard let base = Self.server(baseURL) else {
            signInMessage = "Set the server first."
            return nil
        }
        isSigningIn = true
        let pkce = PKCEPair.generate()
        do {
            let started = try await MotetHTTPClient(configuration: MotetConfiguration(baseURL: base))
                .startNativeSignIn(codeChallenge: pkce.challenge)
            guard let url = URL(string: started.authorizationUrl) else {
                throw NativeSignIn.Failure.notAHandoff
            }
            // Both halves have to agree, and the app's is the one iOS enforces: asking the
            // sheet for an https callback on a host this build is not entitled for is
            // refused outright, so a deployment that turned the flag on ahead of a build
            // falls back to the scheme rather than failing to sign in.
            let entitled = environment.credentials.appLinkDomain
            let host = started.callbackHost.flatMap { $0 == entitled ? $0 : nil }
            return StartedSignIn(
                url: url,
                callbackScheme: started.callbackScheme,
                appLinkHost: host,
                appLinkPath: host == nil ? nil : started.callbackPath,
                pkce: pkce
            )
        } catch {
            isSigningIn = false
            signInMessage = Self.describe(error)
            return nil
        }
    }

    /// Redeem the handoff link the sheet returned, and keep the server and the session it buys.
    func finishSignIn(callback: URL, pkce: PKCEPair, baseURL: String) async {
        defer { isSigningIn = false }
        do {
            guard let base = Self.server(baseURL) else { throw NativeSignIn.Failure.notAHandoff }
            let code = try NativeSignIn.handoffCode(from: callback)
            let session = try await MotetHTTPClient(configuration: MotetConfiguration(baseURL: base))
                .redeemNativeSignIn(code: code, codeVerifier: pkce.verifier)
            guard let token = session.token else { throw NativeSignIn.Failure.noSession }
            // The server the session belongs to is saved with it, so a typed-but-unsaved URL
            // cannot leave the app holding one server's session against another.
            environment.credentials.save(baseURL: baseURL, apiToken: token)
            environment.credentials.saveSession(token: token, email: session.email)
            signedInEmail = session.email
            await applyCredentialChange()
        } catch {
            signInMessage = Self.describe(error)
        }
    }

    /// What the sign-in sheet needs: where to go, and which link closes it.
    ///
    /// `appLinkHost` is set only where the deployment serves an app-site-association file
    /// naming this app *and* this build carries the matching entitlement. It is the stronger
    /// callback — Apple hands such a link to no other app — and the scheme is what iOS 17.3
    /// and older, an unentitled build, or a deployment without that file fall back to.
    struct StartedSignIn {
        let url: URL
        let callbackScheme: String
        let appLinkHost: String?
        let appLinkPath: String?
        let pkce: PKCEPair
    }

    /// The sheet closed without a link: cancelled (no message) or failed (say why).
    func abandonSignIn(_ error: Error?) {
        isSigningIn = false
        signInMessage = error.map(Self.describe)
    }

    /// Revoke the session on the server where possible, and forget it here regardless.
    func signOut() async {
        try? await MotetHTTPClient(configuration: environment.credentials.configuration()).signOut()
        environment.credentials.clearSession()
        signedInEmail = nil
        await applyCredentialChange()
    }

    private static func server(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.host != nil else { return nil }
        return url
    }

    private static func describe(_ error: Error) -> String {
        if let error = error as? MotetError { return error.description }
        if let error = error as? NativeSignIn.Failure { return error.description }
        return error.localizedDescription
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
