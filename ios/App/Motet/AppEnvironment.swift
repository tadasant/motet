import Foundation
import MotetKit
import MotetPlayback

/// The composition root: the one place the real adapters are chosen.
///
/// A singleton because two things outside SwiftUI's world need it — the app delegate's
/// background-download callback and the CarPlay scene, neither of which can be handed an
/// environment object.
///
/// The credential is a session from the web sign-in, never a baked-in token, because a
/// baked-in token would be a credential in a binary. The server is the build's own unless
/// changed under Advanced; `CredentialStore` owns both.
@MainActor
final class AppEnvironment {
    static let shared = AppEnvironment()

    let downloader: BackgroundEpisodeDownloader
    let audioSession = AudioSessionController()
    let nowPlaying = NowPlayingController()
    let credentials = CredentialStore()
    /// Play Live's microphone and reply player. One for the process, like the engine: it
    /// holds the audio session while a Live session runs and hands it back afterwards.
    private(set) lazy var liveAudio: AVLiveAudio = {
        let audioSession = self.audioSession
        let report: @Sendable (String?) -> Void = { [weak self] message in
            Task { @MainActor in
                guard let self else { return }
                await self.controller.report(audioSessionMessage: message)
            }
        }
        return AVLiveAudio(restoreListening: {
            // Play Live switched the session to `.playAndRecord`; this is the only thing
            // that puts listening back, so a refusal here is a briefing that goes silent
            // *after* a conversation and must not be swallowed either.
            report(audioSession.configure().listenerMessage)
            try? audioSession.activate()
        })
    }()

    private(set) var library: MotetLibrary
    private(set) var controller: PlaybackController

    /// Whether the current `controller` has been wired to the engine and the command centre.
    /// Reset by `reconfigure()`, because a rebuilt controller is a *different* controller.
    private var isActivated = false

    /// One engine for the life of the process. `reconfigure()` rebuilds the API-facing half
    /// when the server changes, but replacing the engine would drop whatever is playing.
    private let engine = AVPlayerPlaybackEngine()

    /// **"Is sound actually coming out", asked of the engine rather than of the events it
    /// emitted.** One for the life of the process, like the engine it reads, and unchanged
    /// by `reconfigure()` for the same reason: pointing the app at another server does not
    /// replace the thing making noise.
    ///
    /// It reads the audio session too, because the other half of a silent phone is a
    /// category iOS refused — and a refusal leaves every signal except the route saying
    /// playback is fine.
    private(set) lazy var playbackProbe = PlaybackProbeRecorder(
        engine: engine, route: audioSession
    )

    /// Set when this launch found an API token an earlier build let somebody paste, and
    /// removed it. Read by `AppModel` to say so, once.
    let removedPastedToken: Bool

    private init() {
        let downloader = BackgroundEpisodeDownloader()
        self.downloader = downloader
        // Before anything is wired from the credentials, and here rather than in `AppModel`,
        // because CarPlay can launch the process with no window scene and no `AppModel` at
        // all — and a token reconciled away after wiring would still be in the controller.
        self.removedPastedToken = credentials.reconcile()
        let wired = Self.wire(
            configuration: credentials.configuration(), downloader: downloader, engine: engine
        )
        self.library = wired.library
        self.controller = wired.controller
    }

    /// Wire the current controller to the audio engine, the audio session, and the remote
    /// command centre. Idempotent, and safe to call from whichever scene happens to start
    /// first — the window scene through `AppModel.start()`, or CarPlay, which iOS can launch
    /// into with no window scene at all.
    ///
    /// A controller that is never activated is the quiet failure this exists to prevent: the
    /// engine holds one event handler, so an unactivated controller sees no position
    /// updates, marks nothing read, and never notices the episode end.
    func activate() async {
        guard !isActivated else { return }
        isActivated = true
        await controller.activate()
        let settings = (try? await library.playbackSettings()) ?? PlaybackSettings()
        await controller.update(settings: settings)
        nowPlaying.attach(to: controller, settings: settings)
        await applyAudioSessionShape()
    }

    /// Put the session into the best shape this phone accepts, and tell the player screen
    /// what that cost — nothing, in the ordinary case.
    ///
    /// Called at startup and again before every `play`, because the shape is process-wide
    /// state that Play Live deliberately changes and a route change can disturb: assuming
    /// the startup call still holds is how a briefing ends up playing under
    /// `.playAndRecord` with no `.defaultToSpeaker`, into the earpiece.
    @discardableResult
    func applyAudioSessionShape() async -> AudioSessionPlan.Outcome {
        let outcome = audioSession.configure()
        // Only a refusal reaches the listener. A lower rung that took is a working player,
        // and putting "no AirPlay 2 grouping" on the player screen in error red would train
        // them to ignore the one line that means the audio will be silent — the concession
        // goes to the `audio-session` log instead.
        await controller.report(audioSessionMessage: outcome.listenerMessage)
        return outcome
    }

    /// Rebuild the API-facing half after the server or the session changes.
    ///
    /// Both halves have to be re-established afterwards: `activate()` re-points the engine
    /// and the command centre at the *new* controller, and the caller re-subscribes to
    /// `controller.snapshots()`. `AppModel.applyCredentialChange` does both.
    func reconfigure() async {
        await controller.unload()
        let wired = Self.wire(
            configuration: credentials.configuration(), downloader: downloader, engine: engine
        )
        library = wired.library
        controller = wired.controller
        isActivated = false
        await activate()
    }

    private static func wire(
        configuration: MotetConfiguration,
        downloader: any EpisodeDownloader,
        engine: any PlaybackEngine
    ) -> (library: MotetLibrary, controller: PlaybackController) {
        let support = applicationSupportDirectory()
        let store = makeStore(in: support)
        let api = MotetHTTPClient(configuration: configuration)
        let offline = makeOfflineLibrary(store: store, in: support, downloader: downloader)
        let outbox = Outbox(store: store)
        let positions = ListeningPositionStore(store: store)
        let readState = ReadStateCoordinator(api: api, outbox: outbox)

        return (
            library: MotetLibrary(
                api: api,
                cache: store,
                offline: offline,
                positions: positions,
                readState: readState
            ),
            controller: PlaybackController(
                engine: engine, positions: positions, readState: readState,
                // The server's position, so listening here moves every other device's resume
                // point and marks stories read there as their segments pass (motet#11).
                reportPosition: { episodeId, listenedThroughMs in
                    try await api.setPlaybackPosition(
                        episodeId: episodeId, listenedThroughMs: listenedThroughMs
                    ).listenedThroughMs
                }
            )
        )
    }

    /// If the container is unwritable there is nothing sane to fall back to *except* memory:
    /// the app still plays, it just forgets. Better than refusing to launch.
    private static func makeStore(in support: URL) -> any KeyValueStore {
        (try? FileKeyValueStore(directory: support.appendingPathComponent("state")))
            ?? InMemoryKeyValueStore()
    }

    /// Same reasoning, one step further: an offline library with nowhere to write files still
    /// lets streaming playback work, so fall back to a temporary directory rather than
    /// crashing on launch.
    private static func makeOfflineLibrary(
        store: any KeyValueStore, in support: URL, downloader: any EpisodeDownloader
    ) -> OfflineLibrary {
        if let library = try? OfflineLibrary(
            store: store,
            directory: support.appendingPathComponent("audio"),
            downloader: downloader
        ) {
            return library
        }
        let fallback = FileManager.default.temporaryDirectory
            .appendingPathComponent("motet-audio", isDirectory: true)
        // swiftlint:disable:next force_try — a temporary directory that cannot be created
        // means the sandbox is broken; there is no fifth option.
        return try! OfflineLibrary(store: store, directory: fallback, downloader: downloader)
    }

    /// `Application Support` rather than `Documents`: episode audio is a cache the app can
    /// rebuild, and it should not appear in the Files app or be uploaded to iCloud.
    private static func applicationSupportDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
            .first ?? FileManager.default.temporaryDirectory
        let directory = base.appendingPathComponent("Motet", isDirectory: true)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory
    }
}
