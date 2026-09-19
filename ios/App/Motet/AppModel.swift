import Combine
import Foundation
import MotetKit
import MotetPlayback
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
    /// The Google account the app is signed in as, or nil when nobody is.
    @Published private(set) var signedInEmail: String?
    @Published private(set) var isSigningIn = false
    /// Why the last sign-in did not finish. Shown under the button, never as a modal.
    @Published private(set) var signInMessage: String?
    /// Play Live, as its session last published it.
    @Published private(set) var live = LiveSnapshot()
    /// Why the loaded episode's audio would not load, as the audio route answered — nil
    /// until it has been asked, and whenever nothing is wrong.
    @Published private(set) var playbackProblem: AudioProblem?

    private let environment: AppEnvironment
    private var snapshotTask: Task<Void, Never>?
    private var liveSession: LiveSession?
    private var liveTask: Task<Void, Never>?
    /// The episode the running Live session is about, and what the player last said about
    /// playing — so only a *change* reaches the session.
    private var liveEpisodeId: String?
    private var lastPlaying: Bool?

    init(environment: AppEnvironment) {
        self.environment = environment
        // `AppEnvironment` has already reconciled the Keychain with the address.
        self.signedInEmail = environment.credentials.signedInEmail
    }

    /// The gate `RootView` asks: nothing but the sign-in screen renders until this is true.
    var isSignedIn: Bool { signedInEmail != nil }

    var library: MotetLibrary { environment.library }
    var controller: PlaybackController { environment.controller }

    var isConfigured: Bool { environment.credentials.configuration().isConfigured }

    /// The Sources screen's client, built from the session in force now — so a sign-out or
    /// a server change is picked up on the next call rather than held in a stale copy.
    var sourcesAPI: any SourcesAPI {
        MotetHTTPClient(configuration: environment.credentials.configuration())
    }

    /// The web app's host, where this build can receive a consent (`ConsentCallback`).
    var appLinkDomain: String? { environment.credentials.appLinkDomain }

    func start() async {
        if environment.removedPastedToken {
            signInMessage = "Motet now signs in with Google. The API token this phone held has been removed."
        }
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
                let previousError = self.playback.errorMessage
                self.playback = snapshot
                nowPlaying.update(with: snapshot)
                // The player's error says nothing about *why*; the audio route does — a file
                // that is gone (a 410 since motet#129), or a feed token rotated since this
                // phone cached it, which asking also replaces. The web player asks the same.
                if snapshot.errorMessage != nil, previousError == nil, !snapshot.isOffline,
                   let episodeId = snapshot.episodeId {
                    self.playbackProblem = await self.library.audioProblem(episodeId: episodeId)
                } else if snapshot.errorMessage == nil, self.playbackProblem != nil {
                    self.playbackProblem = nil
                }
                await self.forwardToLive(snapshot)
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
        playbackProblem = nil
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

    /// Try the loaded episode again, with a freshly fetched feed token.
    func retryPlayback() async {
        guard let episode = episodes.first(where: { $0.id == playback.episodeId }) else { return }
        try? await library.invalidateFeedToken()
        await play(episode: episode)
    }

    /// "Mark listened", as the SPA's shelf has it: every story read, the position at the end.
    func markListened(episode: EpisodeResponse) async {
        do {
            try await library.markListened(episode: episode)
        } catch let error as MotetError {
            connectionMessage = error.description
        } catch {
            connectionMessage = String(describing: error)
        }
        await reloadPositions()
        newsItems = (try? await library.newsItems(forceRefresh: false)) ?? newsItems
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

    /// `newsItemIds` nil is every unread story; a list is exactly those. Returns whether the
    /// server took it, so a sheet can stay open with the picks intact when it did not.
    @discardableResult
    func createEpisode(
        title: String,
        maxDurationMinutes: Int,
        newsItemIds: [String]? = nil,
        keepInBacklog: Bool = false
    ) async -> Bool {
        do {
            _ = try await library.createEpisode(
                title: title,
                maxDurationMs: maxDurationMinutes * 60_000,
                newsItemIds: newsItemIds,
                keepInBacklog: keepInBacklog
            )
            await refresh()
            return true
        } catch let error as MotetError {
            connectionMessage = error.description
        } catch {
            connectionMessage = String(describing: error)
        }
        return false
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

    #if DEBUG
    /// Sample stories and no server, for `ScreenshotFixture`.
    func showScreenshotFixture() {
        newsItems = ScreenshotFixture.newsItems
    }
    #endif

    // MARK: - Settings

    /// Point the app at another server, from Settings → Advanced. A session belongs to the
    /// server that issued it, so a real change revokes it there and signs this phone out —
    /// which puts the sign-in screen back in front.
    func saveServer(_ baseURL: String) async {
        let credentials = environment.credentials
        if signedInEmail != nil, credentials.isDifferentServer(baseURL) {
            try? await MotetHTTPClient(configuration: credentials.configuration()).signOut()
        }
        credentials.saveServer(baseURL)
        signedInEmail = credentials.signedInEmail
        await applyCredentialChange()
    }

    // MARK: - Play Live (motet#93)

    /// The session for the player in force now. Rebuilt after a credential change, because
    /// the controller it pauses and plays is rebuilt with it.
    private func liveSessionForCurrentPlayer() -> LiveSession {
        if let liveSession { return liveSession }
        let audio = environment.liveAudio
        let session = LiveSession(
            api: MotetHTTPClient(configuration: environment.credentials.configuration()),
            narration: controller,
            audio: audio,
            makeTransport: { URLSessionLiveTransport() }
        )
        liveSession = session
        liveTask?.cancel()
        liveTask = Task { [weak self] in
            for await snapshot in await session.snapshots() {
                self?.live = snapshot
            }
        }
        return session
    }

    /// Asked of our API whenever the player opens: whether this deployment has a voice
    /// service. "Not configured" is an answer, shown beside a disabled pill.
    func checkLiveAvailability() async {
        await liveSessionForCurrentPlayer().checkAvailability()
    }

    func startLive() async {
        guard let episodeId = playback.episodeId else { return }
        liveEpisodeId = episodeId
        lastPlaying = playback.isPlaying
        await liveSessionForCurrentPlayer().start(episodeId: episodeId, durationMs: playback.durationMs)
    }

    func stopLive(pauseNarration: Bool = true) async {
        liveEpisodeId = nil
        await liveSession?.stop(pauseNarration: pauseNarration)
    }

    func interruptLive() async { await liveSession?.interrupt() }
    func askLive(_ question: String) async { await liveSession?.ask(question) }
    func resumeLiveNarration() async { await liveSession?.resumeNarration() }

    /// What the player did, for the Live session: a change of playing state, and where it is.
    /// Another episode loaded under a running session ends it — the session is about one.
    private func forwardToLive(_ snapshot: PlaybackSnapshot) async {
        guard let liveSession, live.isRunning else {
            lastPlaying = snapshot.isPlaying
            return
        }
        if let liveEpisodeId, snapshot.episodeId != liveEpisodeId {
            // Pausing here would pause the episode that was just loaded.
            await stopLive(pauseNarration: false)
            return
        }
        if snapshot.isPlaying != lastPlaying {
            lastPlaying = snapshot.isPlaying
            // The briefing running out is not the listener pausing it — the SPA skips its
            // `pause` event when the element has ended, for the same reason.
            let ended = !snapshot.isPlaying && snapshot.durationMs > 0
                && snapshot.positionMs >= snapshot.durationMs - 1_000
            if !ended {
                await liveSession.narrationChanged(isPlaying: snapshot.isPlaying, positionMs: snapshot.positionMs)
            }
        }
        await liveSession.narrationPosition(snapshot.positionMs)
    }

    private func applyCredentialChange() async {
        await stopLive()
        liveTask?.cancel()
        liveTask = nil
        liveSession = nil
        live = LiveSnapshot()
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
    func beginSignIn(baseURL: String, allowAppLink: Bool = true) async -> StartedSignIn? {
        signInMessage = nil
        guard let base = Self.server(baseURL) else {
            signInMessage = "Set the server under Advanced first."
            return nil
        }
        // iOS 17.4 is where `ASWebAuthenticationSession` learned to wait for an https
        // callback at all; before it, and in a build carrying no entitlement, there is
        // nothing to offer the server.
        var appLinkDomain: String?
        if #available(iOS 17.4, *), allowAppLink {
            appLinkDomain = environment.credentials.appLinkDomain
        }
        isSigningIn = true
        let pkce = PKCEPair.generate()
        do {
            // The domain is declared *before* the sign-in starts, because the server has to
            // commit to one shape of handoff link and the browser that later calls the
            // callback knows nothing about this build. Nil — no entitlement, an iOS too old
            // to wait for an https callback, or a retry after one was refused — means both
            // sides use the scheme.
            let started = try await MotetHTTPClient(configuration: MotetConfiguration(baseURL: base))
                .startNativeSignIn(codeChallenge: pkce.challenge, appLinkDomain: appLinkDomain)
            guard let url = URL(string: started.authorizationUrl) else {
                throw NativeSignIn.Failure.notAHandoff
            }
            // The server answers with a host only when it took the one that was offered, so
            // this is agreement rather than a second decision.
            let host = started.callbackHost.flatMap { $0 == appLinkDomain ? $0 : nil }
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
    func finishSignIn(
        callback: URL, pkce: PKCEPair, baseURL: String, appLinkHost: String? = nil
    ) async {
        defer { isSigningIn = false }
        do {
            guard let base = Self.server(baseURL) else { throw NativeSignIn.Failure.notAHandoff }
            let code = try NativeSignIn.handoffCode(from: callback, appLinkHost: appLinkHost)
            let session = try await MotetHTTPClient(configuration: MotetConfiguration(baseURL: base))
                .redeemNativeSignIn(code: code, codeVerifier: pkce.verifier)
            guard let token = session.token else { throw NativeSignIn.Failure.noSession }
            // The server the session belongs to is saved with it, so a typed-but-unsaved URL
            // cannot leave the app holding one server's session against another.
            environment.credentials.saveServer(baseURL)
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

    /// The sheet was refused, or failed before anyone could use it. Never silent: a button
    /// that says "Signing in…" and returns to itself with nothing said is the report that led
    /// here (2026-09-19). The detail is in the `signin` log; this is what a person can act on.
    func signInWindowFailed() {
        isSigningIn = false
        signInMessage = "The sign-in window didn't open. Try again."
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

    /// The server the app talks to, and the build's own, for Settings → Advanced.
    var serverURL: String { environment.credentials.serverURL }
    var defaultServerURL: String? { environment.credentials.defaultServerURL }

    /// Whether Advanced may save `baseURL`, and whether saving it would change anything.
    func isValidServer(_ baseURL: String) -> Bool { environment.credentials.isValidServer(baseURL) }
    func isDifferentServer(_ baseURL: String) -> Bool { environment.credentials.isDifferentServer(baseURL) }

    /// Coming back to the foreground: send whatever the walk queued, and pick playback up
    /// if the system interrupted it politely.
    func handleForeground() async {
        try? await library.flushPendingWrites()
        await controller.resumeAfterInterruptionIfNeeded()
        await refresh()
    }
}
