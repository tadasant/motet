import MotetKit
import SwiftUI

/// The sign-in screen until a session exists; then four tabs, and a player that stays put
/// across all of them.
///
/// The gate is the whole of the app, not a tab: nothing but `SignInView` renders until
/// somebody has signed in (Tadas, 2026-09-19), so no screen ever loads against a server it
/// has no session for.
///
/// The mini-player is not decoration: a listener who taps into the backlog mid-episode must
/// still be able to pause without finding their way back.
struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    @State private var showingPlayer = false
    /// Read so that a text-size change re-renders this view, and used as the tree's identity
    /// below. The brand faces are resolved to a size when a body runs, not tracked by
    /// SwiftUI the way a system text style is, so without a rebuild a size change would
    /// leave every screen at the old size until it happened to redraw.
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        Group {
            if model.isSignedIn {
                tabs
            } else {
                SignInView()
            }
        }
        .safeAreaInset(edge: .top, spacing: 0) { BuildBadge() }
        .id(dynamicTypeSize)
        .sheet(isPresented: $showingPlayer) { PlayerView().id(dynamicTypeSize) }
        .onChange(of: scenePhase) { _, phase in
            // Signed out there is nothing to flush and nobody to refresh for.
            if phase == .active, model.isSignedIn {
                Task { await model.handleForeground() }
            }
        }
    }

    private var tabs: some View {
        VStack(spacing: 0) {
            TabView {
                EpisodesView()
                    .tabItem { Label("Episodes", systemImage: "waveform") }
                BacklogView()
                    .tabItem { Label("Backlog", systemImage: "tray.full") }
                SourcesView()
                    .tabItem { Label("Sources", systemImage: "point.3.connected.trianglepath.dotted") }
                SettingsView()
                    .tabItem { Label("Settings", systemImage: "gearshape") }
            }
            if model.playback.hasEpisode {
                MiniPlayerView(onExpand: { showingPlayer = true })
                    .transition(.move(edge: .bottom))
            }
        }
    }
}

/// A strip naming the deployment, on every build that is not the real one.
///
/// **Production shows nothing, and that asymmetry is deliberate.** The badge exists so
/// nobody trusts staging data or reports a staging bug against production; a permanent
/// chip over the real app would be noise, and — worse — a build that guessed wrong would
/// put "this is disposable" over somebody's actual backlog. So `BuildEnvironment` reads an
/// absent or unrecognised label as production, and this renders nothing for it.
///
/// It names no hostname of its own: the label is a build setting that carries no topology,
/// and the host beside it is whatever this device resolved.
struct BuildBadge: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let target = model.buildTarget
        if let badge = target.environment.badge {
            HStack(spacing: 6) {
                Text(badge)
                    .font(Theme.body(11, weight: 700))
                    .tracking(0.6)
                Text(target.host ?? "no server")
                    .font(.system(size: 11, design: .monospaced))
                    .lineLimit(1)
                    .truncationMode(.head)
            }
            .foregroundStyle(Theme.parchment)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 3)
            .background(Theme.ink)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("build-badge")
            .accessibilityLabel("\(badge) build on \(target.host ?? "no server")")
        }
    }
}

/// The offline / error banner. One line, never a modal: being offline is an ordinary state
/// for this app, not a failure to interrupt someone over.
struct ConnectionBanner: View {
    let message: String?

    var body: some View {
        if let message {
            Label(message, systemImage: "wifi.slash")
                .font(Theme.body(13, weight: 500, relativeTo: .footnote))
                .foregroundStyle(Theme.inkSoft)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal)
                .padding(.vertical, 8)
                .background(Theme.surface)
                .overlay(alignment: .bottom) {
                    Rectangle().fill(Theme.rule).frame(height: 1)
                }
        }
    }
}
