import MotetKit
import SwiftUI

/// The player. Every control here is a `PlaybackCommand` — the same set the lockscreen and
/// CarPlay send, and none of them goes near a model or the network.
struct PlayerView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var isScrubbing = false
    @State private var scrubPositionMs: Double = 0

    private var episode: EpisodeResponse? {
        model.episodes.first { $0.id == model.playback.episodeId }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                VStack(spacing: 8) {
                    Text(model.playback.episodeTitle)
                        .font(.title3.weight(.semibold))
                        .multilineTextAlignment(.center)
                    if let segment = model.playback.currentSegmentTitle {
                        Text(segment).font(.subheadline).foregroundStyle(.secondary)
                    }
                    if model.playback.isOffline {
                        Label("Playing from this device", systemImage: "arrow.down.circle.fill")
                            .font(.caption)
                            .foregroundStyle(.green)
                    }
                }
                .padding(.top, 24)

                scrubber
                transportControls
                speedControl

                if let episode {
                    TranscriptList(episode: episode, positionMs: model.playback.positionMs)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal)
            .navigationTitle("Now playing")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    /// The scrubber seeks when the thumb is *released*, not on every frame of the drag.
    ///
    /// Seeking continuously would issue an `AVPlayer` seek and an atomic position write per
    /// frame, and the thumb would fight the user: the binding's `get` reads the position the
    /// player last reported, which lags the finger.
    private var scrubber: some View {
        VStack(spacing: 4) {
            Slider(
                value: Binding(
                    get: { isScrubbing ? scrubPositionMs : Double(model.playback.positionMs) },
                    set: { scrubPositionMs = $0 }
                ),
                in: 0...Double(max(model.playback.durationMs, 1)),
                onEditingChanged: { editing in
                    if editing {
                        scrubPositionMs = Double(model.playback.positionMs)
                        isScrubbing = true
                    } else {
                        isScrubbing = false
                        Task { await model.perform(.seek(toMs: Int(scrubPositionMs))) }
                    }
                }
            )
            HStack {
                Text(Format.time(isScrubbing ? Int(scrubPositionMs) : model.playback.positionMs))
                Spacer()
                Text("−" + Format.time(max(0, model.playback.durationMs - model.playback.positionMs)))
            }
            .font(.caption.monospacedDigit())
            .foregroundStyle(.secondary)
        }
    }

    private var transportControls: some View {
        HStack(spacing: 28) {
            command(.previousSegment, systemImage: "backward.end.fill", label: "Previous story")
            command(.skipBackward, systemImage: "gobackward.15", label: "Back 15 seconds")
            Button {
                Task { await model.perform(.togglePlayPause) }
            } label: {
                Image(systemName: model.playback.isPlaying ? "pause.circle.fill" : "play.circle.fill")
                    .resizable()
                    .frame(width: 68, height: 68)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(model.playback.isPlaying ? "Pause" : "Play")
            command(.skipForward, systemImage: "goforward.30", label: "Forward 30 seconds")
            command(.nextSegment, systemImage: "forward.end.fill", label: "Next story")
        }
    }

    private var speedControl: some View {
        Picker("Speed", selection: Binding(
            get: { model.settings.rate },
            set: { newValue in Task { await model.perform(.setRate(newValue)) } }
        )) {
            ForEach(PlaybackSettings.rateLadder, id: \.self) { rate in
                Text(Format.rate(rate)).tag(rate)
            }
        }
        .pickerStyle(.segmented)
    }

    private func command(_ command: PlaybackCommand, systemImage: String, label: String) -> some View {
        Button {
            Task { await model.perform(command) }
        } label: {
            Image(systemName: systemImage).font(.title2)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// The stories in this episode, with the claims behind them.
///
/// Every claim carries the span it came from (invariant 3), and showing them side by side is
/// what makes that checkable rather than merely true — so the transcript renders the source
/// excerpt beside what was spoken.
struct TranscriptList: View {
    @EnvironmentObject private var model: AppModel
    let episode: EpisodeResponse
    let positionMs: Int

    var body: some View {
        List {
            ForEach(Array(episode.segments.enumerated()), id: \.offset) { _, segment in
                Section {
                    Button {
                        Task { await model.perform(.seek(toMs: segment.startMs)) }
                    } label: {
                        HStack {
                            Text(segment.newsItemTitle).font(.subheadline.weight(.medium))
                            Spacer()
                            Text(Format.time(segment.startMs))
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)

                    ForEach(Array(segment.claims.enumerated()), id: \.offset) { _, claim in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(claim.text).font(.footnote)
                            Text("“\(claim.sourceExcerpt)” — \(claim.sourceTitle)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .listRowBackground(
                    isCurrent(segment) ? Color.accentColor.opacity(0.12) : Color.clear
                )
            }
        }
        .listStyle(.plain)
    }

    private func isCurrent(_ segment: SegmentResponse) -> Bool {
        positionMs >= segment.startMs && positionMs < segment.startMs + segment.durationMs
    }
}

/// The bar above the tabs.
struct MiniPlayerView: View {
    @EnvironmentObject private var model: AppModel
    let onExpand: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Button(action: onExpand) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.playback.currentSegmentTitle ?? model.playback.episodeTitle)
                        .font(.subheadline.weight(.medium))
                        .lineLimit(1)
                    Text(
                        "\(Format.time(model.playback.positionMs)) · \(Format.rate(model.playback.rate))"
                    )
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.plain)

            Button {
                Task { await model.perform(.skipBackward) }
            } label: {
                Image(systemName: "gobackward.15")
            }
            .accessibilityLabel("Back 15 seconds")

            Button {
                Task { await model.perform(.togglePlayPause) }
            } label: {
                Image(systemName: model.playback.isPlaying ? "pause.fill" : "play.fill")
                    .font(.title3)
            }
            .accessibilityLabel(model.playback.isPlaying ? "Pause" : "Play")

            Button {
                Task { await model.perform(.skipForward) }
            } label: {
                Image(systemName: "goforward.30")
            }
            .accessibilityLabel("Forward 30 seconds")
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.thinMaterial)
    }
}
