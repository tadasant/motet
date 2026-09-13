import MotetKit
import SwiftUI

/// Three tabs, and a player that stays put across all of them.
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
        .id(dynamicTypeSize)
        .sheet(isPresented: $showingPlayer) { PlayerView().id(dynamicTypeSize) }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task { await model.handleForeground() }
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
