import MotetKit
import SwiftUI

/// The listening surface: what there is to hear, and what is already on the phone.
struct EpisodesView: View {
    @EnvironmentObject private var model: AppModel
    @State private var isCreating = false

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
        }
    }
}

struct EpisodeRow: View {
    @EnvironmentObject private var model: AppModel
    let episode: EpisodeResponse
    let position: ListeningPosition?
    let isDownloaded: Bool

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
                        Text("· \(episode.episodeState.displayName)")
                    }
                    if isDownloaded {
                        Label("Downloaded", systemImage: "arrow.down.circle.fill")
                            .labelStyle(.iconOnly)
                            .accessibilityLabel("Downloaded")
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

                if let error = episode.lastError, episode.episodeState == .failed {
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
        .swipeActions(edge: .leading) {
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
    @State private var title = "Episode — \(Date.now.formatted(date: .abbreviated, time: .omitted))"
    @State private var minutes = 20

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Title", text: $title)
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
                            await model.createEpisode(title: title, maxDurationMinutes: minutes)
                            dismiss()
                        }
                    }
                    .disabled(title.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
        .preferredColorScheme(.light)
        .tint(Theme.ink)
    }
}
