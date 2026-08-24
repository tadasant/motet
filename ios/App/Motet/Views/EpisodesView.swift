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
                        ContentUnavailableView(
                            "No episodes yet",
                            systemImage: "waveform",
                            description: Text("Make one from everything unread in your backlog.")
                        )
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
                .refreshable { await model.refresh() }
            }
            .navigationTitle("Motet")
            .toolbar {
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

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Button {
                Task { await model.play(episode: episode) }
            } label: {
                Image(systemName: episode.episodeState.isPlayable ? "play.circle.fill" : "clock")
                    .font(.title)
                    .foregroundStyle(episode.episodeState.isPlayable ? Color.accentColor : .secondary)
            }
            .buttonStyle(.plain)
            .disabled(!episode.episodeState.isPlayable)
            .accessibilityLabel("Play \(episode.title)")

            VStack(alignment: .leading, spacing: 4) {
                Text(episode.title).font(.headline)
                HStack(spacing: 6) {
                    Text(Format.duration(episode.durationMs))
                    if !episode.episodeState.isPlayable {
                        Text("· \(episode.episodeState.displayName)")
                    }
                    if isDownloaded {
                        Label("Downloaded", systemImage: "arrow.down.circle.fill")
                            .labelStyle(.iconOnly)
                            .foregroundStyle(.green)
                            .accessibilityLabel("Downloaded")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)

                if let position, position.spokenThroughMs > 0 {
                    ProgressView(value: position.fraction)
                        .tint(position.isFinished ? .secondary : .accentColor)
                    Text(
                        position.isFinished
                            ? "Finished"
                            : "\(Format.time(position.spokenThroughMs)) of \(Format.time(position.durationMs))"
                    )
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                }

                if let error = episode.lastError, episode.episodeState == .failed {
                    Text(error).font(.caption2).foregroundStyle(.red)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 4)
        .swipeActions(edge: .trailing) {
            if isDownloaded {
                Button(role: .destructive) {
                    Task { await model.removeDownload(episode: episode) }
                } label: {
                    Label("Remove", systemImage: "trash")
                }
            } else if episode.episodeState.isPlayable {
                Button {
                    Task { await model.download(episode: episode) }
                } label: {
                    Label("Download", systemImage: "arrow.down.circle")
                }
                .tint(.blue)
            }
        }
    }
}

/// Phase 1's episode shape: everything unread, capped by how long the walk is.
struct NewEpisodeView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var title = "Briefing"
    @State private var minutes = 20

    var body: some View {
        NavigationStack {
            Form {
                TextField("Title", text: $title)
                Stepper("Up to \(minutes) minutes", value: $minutes, in: 5...90, step: 5)
            }
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
    }
}
