import CarPlay
import MotetKit
import SwiftUI
import UIKit

/// The app.
///
/// Everything below this line is a rendering of `MotetKit`: the screens hold no playback
/// rules, no read-state rules, and no networking. That is why the logic is testable on a
/// machine with no Xcode on it, and why a CarPlay template and a SwiftUI button can be the
/// same command.
@main
@MainActor
struct MotetApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel(environment: AppEnvironment.shared)

    init() {
        // Before the first view renders, so no screen is ever drawn in a fallback face
        // that the real one then replaces.
        BrandFont.register()
        Theme.applyBarAppearance()
    }

    var body: some Scene {
        WindowGroup {
            root
                .environmentObject(model)
                // Dark mode is not designed (brand/GUIDELINES.md), so hold the app in light
                // mode rather than let the system invert a palette nobody chose.
                .preferredColorScheme(.light)
                .tint(Theme.ink)
                .task {
                    #if DEBUG
                    // Both fixtures render without a server and without a session, so
                    // neither may take the path that needs one.
                    if ScreenshotFixture.current != nil { return }
                    if PlaybackProbeFixture.isRequested { return }
                    #endif
                    await model.start()
                }
        }
    }
}

extension MotetApp {
    @ViewBuilder
    private var root: some View {
        #if DEBUG
        if let fixture = ScreenshotFixture.current {
            BacklogView(fixture: fixture)
                .task { model.showScreenshotFixture() }
        } else if PlaybackProbeFixture.isRequested {
            PlaybackProbeView()
        } else {
            RootView()
        }
        #else
        RootView()
        #endif
    }
}

/// Three things need an app delegate, and none has a SwiftUI equivalent.
@MainActor
final class AppDelegate: NSObject, UIApplicationDelegate {
    /// The mark as Now Playing artwork, for the lockscreen and CarPlay. Set here rather than
    /// in a scene because iOS can launch straight into CarPlay with no window scene.
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        AppEnvironment.shared.nowPlaying.artwork = BrandArtwork.nowPlaying()
        return true
    }

    /// iOS wakes the app when a background download finishes while it is suspended, and
    /// expects the handler to be called once the app has dealt with the events.
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        AppEnvironment.shared.downloader.backgroundCompletionHandler = completionHandler
    }

    /// The CarPlay scene is declared in Info.plist and given its delegate here.
    ///
    /// The window role is deliberately answered with an *unnamed* configuration: this is a
    /// SwiftUI-lifecycle app, and naming a configuration the Info.plist manifest does not
    /// define would hand back a scene with no delegate and no SwiftUI content — a black
    /// screen. Unnamed means "the default for this role", which is SwiftUI's own.
    func application(
        _ application: UIApplication,
        configurationForConnecting connectingSceneSession: UISceneSession,
        options: UIScene.ConnectionOptions
    ) -> UISceneConfiguration {
        guard connectingSceneSession.role == .carTemplateApplication else {
            return UISceneConfiguration(name: nil, sessionRole: connectingSceneSession.role)
        }
        let configuration = UISceneConfiguration(
            name: "CarPlay", sessionRole: connectingSceneSession.role
        )
        configuration.delegateClass = CarPlaySceneDelegate.self
        return configuration
    }
}
