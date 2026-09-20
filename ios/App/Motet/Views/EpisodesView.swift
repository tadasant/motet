import MotetKit
import SwiftUI

/// The listening surface: what there is to hear, and what is already on the phone.
struct EpisodesView: View {
    /// How often to re-read while an episode is being built, and while one is stuck.
    /// A build nothing will run moves when a worker appears, which is not a two-second
    /// question — so the watch slows rather than polling a stall forever (motet#136's
    /// rule, one pipeline along).
    private static let buildPoll: Duration = .seconds(2)
    private static let stalledPoll: Duration = .seconds(10)

    @EnvironmentObject private var model: AppModel
    @State private var isCreating = false

    /// Whether any episode is still being made, which is what the watch runs on.
    private var building: Bool { model.episodes.contains(where: \.isBuilding) }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ConnectionBanner(message: model.connectionMessage)
                List {
                    if model.episodes.isEmpty {
                        ContentUnavailableView {
                            VStack(spacing: 12) {
                                VoiceDots(size: 8)
                                Text("No episodes yet")
                                    .font(Theme.display(26, relativeTo: .title2))
                                    .foregroundStyle(Theme.ink)
                            }
                        } description: {
                            Text("Make one from everything unread in your backlog.")
                                .font(Theme.body(15, relativeTo: .subheadline))
                                .foregroundStyle(Theme.inkSoft)
                        }
                        .listRowBackground(Theme.parchment)
                        .listRowSeparator(.hidden)
                    }
                    ForEach(model.episodes, id: \.id) { episode in
                        EpisodeRow(
                            episode: episode,
                            position: model.positions[episode.id],
                            isDownloaded: model.downloadedEpisodeIds.contains(episode.id)
                        )
                    }
                }
                .listStyle(.plain)
                .brandGround()
                .refreshable { await model.refresh() }
            }
            .background(Theme.parchment)
            // The screen's title is the wordmark: lowercase, Fraunces italic, never the sans.
            .navigationTitle("Motet")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Wordmark(size: 26)
                }
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        isCreating = true
                    } label: {
                        Label("New episode", systemImage: "plus")
                    }
                }
            }
            .sheet(isPresented: $isCreating) { NewEpisodeView() }
            .task(id: building) { await watchBuilds() }
        }
    }

    /// Re-read for as long as the server says an episode is being built — not for a fixed
    /// window. A first episode off a full backlog is minutes of work, and the progress is
    /// the server's, so it can be watched to the end. Before this the phone showed
    /// "Queued" until somebody pulled to refresh.
    private func watchBuilds() async {
        while !Task.isCancelled, building {
            let moving = model.episodes.contains { EpisodeProgress.moving($0.build) }
            try? await Task.sleep(for: moving ? Self.buildPoll : Self.stalledPoll)
            guard !Task.isCancelled else { return }
            await model.refresh()
        }
    }
}

struct EpisodeRow: View {
    @EnvironmentObject private var model: AppModel
    let episode: EpisodeResponse
    let position: ListeningPosition?
    let isDownloaded: Bool
    /// The rename sheet's field, and whether it is up. An episode is named after the day it
    /// was made unless somebody typed something, so most of them want a better name later.
    @State private var isRenaming = false
    @State private var draftTitle = ""

    /// Heard to the end here, or within the sign-off's slack of it anywhere.
    private var isListened: Bool {
        position?.isFinished == true
            || (episode.durationMs > 0 && episode.durationMs - episode.listenedThroughMs <= 5_000)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Button {
                Task { await model.play(episode: episode) }
            } label: {
                Image(systemName: episode.episodeState.isPlayable ? "play.fill" : "clock")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(episode.episodeState.isPlayable ? Theme.parchment : Theme.inkMute)
                    .frame(width: 36, height: 36)
                    .background(
                        Circle().fill(episode.episodeState.isPlayable ? Theme.ink : Theme.surface)
                    )
            }
            .buttonStyle(.plain)
            .disabled(!episode.episodeState.isPlayable)
            .accessibilityLabel("Play \(episode.title)")

            VStack(alignment: .leading, spacing: 6) {
                Text(episode.title)
                    .font(Theme.display(19, relativeTo: .headline))
                    .foregroundStyle(Theme.ink)
                HStack(spacing: 6) {
                    Text(Format.duration(episode.durationMs))
                    if !episode.episodeState.isPlayable {
                        // The bare state word — "Queued", "Recording" — was the whole of
                        // what a row said about a build, which is the complaint this is
                        // fixing. What it says now is the step and the clock, from the
                        // server; the state word is the fallback for an API without one.
                        if let build = episode.build {
                            Text("· \(EpisodeProgress.describeRow(build))")
                        } else {
                            Text("· \(episode.episodeState.displayName)")
                        }
                    }
                    if isDownloaded {
                        Label("Downloaded", systemImage: "arrow.down.circle.fill")
                            .labelStyle(.iconOnly)
                            .accessibilityLabel("Downloaded")
                    }
                    if episode.keepsStoriesInBacklog {
                        // Said on the row, because it changes what listening does.
                        Text("· Stays in backlog")
                    }
                }
                .font(Theme.body(13, weight: 500, relativeTo: .caption))
                .monospacedDigit()
                .foregroundStyle(Theme.inkSoft)

                if let position, position.spokenThroughMs > 0 {
                    PlayedTrack(
                        fraction: position.fraction, height: 4, isFinished: position.isFinished
                    )
                    Text(
                        position.isFinished
                            ? "Finished"
                            : "\(Format.time(position.spokenThroughMs)) of \(Format.time(position.durationMs))"
                    )
                    .font(Theme.body(12, weight: 500, relativeTo: .caption2))
                    .monospacedDigit()
                    .foregroundStyle(Theme.inkSoft)
                } else if episode.listenedThroughMs > 0, episode.durationMs > 0 {
                    // Never played on this phone, but listened to somewhere: the server's
                    // position, which is where Play resumes (motet#11).
                    let finished = episode.durationMs - episode.listenedThroughMs <= 5_000
                    PlayedTrack(
                        fraction: Double(episode.listenedThroughMs) / Double(episode.durationMs),
                        height: 4, isFinished: finished
                    )
                    Text(
                        finished
                            ? "Listened"
                            : "\(Format.time(episode.listenedThroughMs)) of \(Format.time(episode.durationMs))"
                    )
                    .font(Theme.body(12, weight: 500, relativeTo: .caption2))
                    .monospacedDigit()
                    .foregroundStyle(Theme.inkSoft)
                }

                // The full reading — the bar, the counts, the estimate and the remedy —
                // under the row it belongs to. The phone has no episode detail screen, so
                // there is nowhere else for it to be; a ready episode does not get one,
                // because the duration beside the title is already the whole answer.
                if let build = episode.build,
                   EpisodeProgress.inFlight(build) || build.stage == "failed" {
                    EpisodeProgressView(description: EpisodeProgress.describe(build))
                } else if let error = episode.lastError, episode.episodeState == .failed {
                    // An API older than `build_progress`: the failure still has to read.
                    Text(error)
                        .font(Theme.body(12, relativeTo: .caption2))
                        .foregroundStyle(Theme.errorText)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(14)
        .brandCard()
        .listRowBackground(Theme.parchment)
        .listRowSeparator(.hidden)
        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
        .contextMenu {
            Button {
                draftTitle = episode.title
                isRenaming = true
            } label: {
                Label("Rename", systemImage: "pencil")
            }
        }
        .alert("Rename episode", isPresented: $isRenaming) {
            TextField("Title", text: $draftTitle)
            Button("Cancel", role: .cancel) {}
            Button("Save") {
                let title = draftTitle.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !title.isEmpty, title != episode.title else { return }
                Task { await model.rename(episode: episode, to: title) }
            }
        }
        .swipeActions(edge: .leading) {
            Button {
                draftTitle = episode.title
                isRenaming = true
            } label: {
                Label("Rename", systemImage: "pencil")
            }
            .tint(Theme.inkSoft)
            if episode.episodeState.isPlayable, !isListened {
                Button {
                    Task { await model.markListened(episode: episode) }
                } label: {
                    Label("Mark listened", systemImage: "checkmark.circle")
                }
                .tint(Theme.ink)
            }
        }
        .swipeActions(edge: .trailing) {
            if isDownloaded {
                Button(role: .destructive) {
                    Task { await model.removeDownload(episode: episode) }
                } label: {
                    Label("Remove", systemImage: "trash")
                }
                .tint(Theme.error)
            } else if episode.episodeState.isPlayable {
                Button {
                    Task { await model.download(episode: episode) }
                } label: {
                    Label("Download", systemImage: "arrow.down.circle")
                }
                .tint(Theme.ink)
            }
        }
    }
}

/// Phase 1's episode shape: everything unread, capped by how long the walk is.
struct NewEpisodeView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    /// Empty, and sent as nothing: the server names an episode after the day it was made,
    /// and it is the only place that string is composed. Renaming is a swipe on the row.
    @State private var title = ""
    @State private var minutes = 20

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Title", text: $title, prompt: Text("Today's date"))
                        .font(Theme.body(16))
                    Stepper("Up to \(minutes) minutes", value: $minutes, in: 5...90, step: 5)
                        .font(Theme.body(16))
                        .monospacedDigit()
                }
                .listRowBackground(Theme.surface)
            }
            .brandGround()
            .foregroundStyle(Theme.ink)
            .navigationTitle("New episode")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Make it") {
                        Task {
                            await model.createEpisode(
                                title: title.trimmingCharacters(in: .whitespacesAndNewlines)
                                    .nilIfEmpty,
                                maxDurationMinutes: minutes
                            )
                            dismiss()
                        }
                    }
                }
            }
        }
        .preferredColorScheme(.light)
        .tint(Theme.ink)
    }
}
