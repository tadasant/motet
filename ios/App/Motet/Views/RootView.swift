import MotetKit
import SwiftUI

/// The sign-in screen until a session exists; then three tabs, and a player that stays put
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
