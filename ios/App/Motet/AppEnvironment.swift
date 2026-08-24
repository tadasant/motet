import Foundation
import MotetKit
import MotetPlayback

/// The composition root: the one place the real adapters are chosen.
///
/// A singleton because two things outside SwiftUI's world need it — the app delegate's
/// background-download callback and the CarPlay scene, neither of which can be handed an
/// environment object.
///
/// Nothing here is configured at build time. The server URL and the token are typed into
/// Settings on first run: this repo is public, so a baked-in hostname would be an
/// infrastructure fact in it and a baked-in token would be a credential in a binary.
@MainActor
final class AppEnvironment {
    static let shared = AppEnvironment()

    let downloader: BackgroundEpisodeDownloader
    let audioSession = AudioSessionController()
    let nowPlaying = NowPlayingController()
    let credentials = CredentialStore()

    private(set) var library: MotetLibrary
    private(set) var controller: PlaybackController

    /// Whether the current `controller` has been wired to the engine and the command centre.
    /// Reset by `reconfigure()`, because a rebuilt controller is a *different* controller.
    private var isActivated = false

    /// One engine for the life of the process. `reconfigure()` rebuilds the API-facing half
    /// when the server changes, but replacing the engine would drop whatever is playing.
    private let engine = AVPlayerPlaybackEngine()

    private init() {
        let downloader = BackgroundEpisodeDownloader()
        self.downloader = downloader
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
        try? audioSession.configure()
    }

    /// Rebuild the API-facing half after the server URL or token changes in Settings.
    ///
    /// Both halves have to be re-established afterwards: `activate()` re-points the engine
    /// and the command centre at the *new* controller, and the caller re-subscribes to
    /// `controller.snapshots()`. `AppModel.saveCredentials` does both.
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
                engine: engine, positions: positions, readState: readState
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
