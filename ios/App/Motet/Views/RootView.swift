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
        .sheet(isPresented: $showingPlayer) { PlayerView() }
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
                .font(.footnote)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal)
                .padding(.vertical, 6)
                .background(.thinMaterial)
        }
    }
}
