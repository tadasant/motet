import MotetKit
import SwiftUI

/// The player. Every control here is a `PlaybackCommand` — the same set the lockscreen and
/// CarPlay send, and none of them goes near a model or the network.
struct PlayerView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    /// Where a drag has the knob, or nil when nothing is being dragged. Cleared by the
    /// track itself when its gesture ends *or is cancelled* — see `ScrubberTrack`.
    @State private var scrubFraction: Double?

    private var episode: EpisodeResponse? {
        model.episodes.first { $0.id == model.playback.episodeId }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                VStack(spacing: 8) {
                    Text(model.playback.episodeTitle)
                        .font(Theme.display(24, relativeTo: .title3))
                        .foregroundStyle(Theme.ink)
                        .multilineTextAlignment(.center)
                    if let segment = model.playback.currentSegmentTitle {
                        Text(segment)
                            .font(Theme.aside(15, relativeTo: .subheadline))
                            .foregroundStyle(Theme.inkSoft)
                            .multilineTextAlignment(.center)
                    }
                    if model.playback.isOffline {
                        Label("Playing from this device", systemImage: "arrow.down.circle.fill")
                            .brandLabel(size: 11)
                    }
                    loadState
                }
                .padding(.top, 24)

                scrubber
                transportControls
                pills
                LivePanel()

                if let episode {
                    TranscriptList(episode: episode, positionMs: model.playback.positionMs)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal)
            .background(Theme.parchment.ignoresSafeArea())
            .navigationTitle("Now playing")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .preferredColorScheme(.light)
        .tint(Theme.ink)
    }

    /// The scrubber seeks when the knob is *released*, not on every frame of the drag.
    ///
    /// Seeking continuously would issue an `AVPlayer` seek and an atomic position write per
    /// frame, and the knob would fight the user: the position the player last reported lags
    /// the finger.
    private var scrubber: some View {
        let durationMs = Double(max(model.playback.durationMs, 1))
        let shownMs = scrubFraction.map { $0 * durationMs } ?? Double(model.playback.positionMs)
        return VStack(spacing: 6) {
            ScrubberTrack(
                fraction: shownMs / durationMs,
                onDragChanged: { fraction in
                    scrubFraction = fraction
                },
                onDragEnded: { fraction in
                    let target = Int(fraction * durationMs)
                    scrubFraction = nil
                    Task { await model.perform(.seek(toMs: target)) }
                }
            )
            .accessibilityElement()
            .accessibilityLabel("Position")
            .accessibilityValue(
                "\(Format.time(Int(shownMs))) of \(Format.time(model.playback.durationMs))"
            )
            .accessibilityAdjustableAction { direction in
                switch direction {
                case .increment: Task { await model.perform(.skipForward) }
                case .decrement: Task { await model.perform(.skipBackward) }
                @unknown default: break
                }
            }

            HStack {
                Text(Format.time(Int(shownMs)))
                Spacer()
                Text("−" + Format.time(max(0, model.playback.durationMs - Int(shownMs))))
            }
            .font(Theme.body(13, weight: 500, relativeTo: .caption))
            .monospacedDigit()
            .foregroundStyle(Theme.inkSoft)
            .accessibilityHidden(true)
        }
    }

    private var transportControls: some View {
        HStack(spacing: 28) {
            command(.previousSegment, systemImage: "backward.end.fill", label: "Previous story")
            command(.skipBackward, systemImage: "gobackward.15", label: "Back 15 seconds")
            Button {
                Task { await model.perform(.togglePlayPause) }
            } label: {
                Image(systemName: model.playback.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: 18, weight: .bold))
                    .foregroundStyle(Theme.parchment)
                    // The play triangle's visual centre sits right of its box.
                    .offset(x: model.playback.isPlaying ? 0 : 1.5)
                    .frame(width: 46, height: 46)
                    .background(Circle().fill(Theme.ink))
                    .shadow(color: Theme.ink.opacity(0.35), radius: 10, y: 6)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(model.playback.isPlaying ? "Pause" : "Play")
            command(.skipForward, systemImage: "goforward.30", label: "Forward 30 seconds")
            command(.nextSegment, systemImage: "forward.end.fill", label: "Next story")
        }
    }

    /// Loading, waiting, failing, and a session this phone would not grant — said where
    /// they can be seen. The controller always knew the first and the last two; the screen
    /// showed none of them, so audio that would not load was a play button that did nothing.
    ///
    /// The middle one is new and is the one the TestFlight report was about: `AVPlayer`
    /// waiting to start is neither loading nor failing, and it was the state nothing
    /// anywhere had a name for.
    @ViewBuilder
    private var loadState: some View {
        if let session = model.playback.audioSessionMessage {
            // Above the rest deliberately: it is the reason *any* episode would be silent,
            // so a listener chasing silence should meet it before an episode-specific line.
            Text(session)
                .font(Theme.body(12, relativeTo: .caption))
                .foregroundStyle(Theme.errorText)
                .multilineTextAlignment(.center)
                .padding(.top, 4)
        }
        if let error = model.playback.errorMessage {
            VStack(spacing: 6) {
                Text("This episode’s audio could not be loaded.")
                    .font(Theme.body(14, weight: 600, relativeTo: .footnote))
                    .foregroundStyle(Theme.errorText)
                    .multilineTextAlignment(.center)
                // The route's answer when there is one — "no longer in storage" is not a
                // retry away — and the player's own words only when there is not.
                Text(model.playbackProblem?.sentence ?? error)
                    .font(Theme.body(12, relativeTo: .caption))
                    .foregroundStyle(Theme.inkSoft)
                    .lineLimit(3)
                    .multilineTextAlignment(.center)
                if !isGone {
                    Button("Try again") { Task { await model.retryPlayback() } }
                        .font(Theme.body(14, weight: 600))
                }
            }
            .padding(.top, 4)
        } else if let stall = model.playback.stallMessage {
            VStack(spacing: 4) {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    Text("Waiting for audio…")
                        .font(Theme.body(13, relativeTo: .footnote))
                        .foregroundStyle(Theme.inkSoft)
                }
                Text(stall)
                    .font(Theme.body(12, relativeTo: .caption))
                    .foregroundStyle(Theme.inkSoft)
                    .multilineTextAlignment(.center)
            }
        } else if model.playback.isLoading {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Loading audio…")
                    .font(Theme.body(13, relativeTo: .footnote))
                    .foregroundStyle(Theme.inkSoft)
            }
        }
    }

    private var isGone: Bool {
        if case .gone = model.playbackProblem { return true }
        return false
    }

    /// The speed pill and the mic pill, as the brand's transport draws them.
    private var pills: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                speedControl
                LivePill()
            }
            // Said where it can be seen, not only to VoiceOver: a dimmed pill with no reason
            // beside it reads as a control that is broken.
            if case .unavailable(let reason) = model.live.availability {
                Text(reason)
                    .font(Theme.body(12, relativeTo: .caption))
                    .foregroundStyle(Theme.inkSoft)
                    .multilineTextAlignment(.center)
            } else if !model.live.isRunning, model.live.availability == .available {
                Text("Headphones recommended: the mic stays open for the whole session.")
                    .font(Theme.body(12, relativeTo: .caption))
                    .foregroundStyle(Theme.inkSoft)
            }
        }
        .task { await model.checkLiveAvailability() }
    }

    /// The same choice the segmented picker offered — any rung of the rate ladder — drawn
    /// as a pill that opens it.
    private var speedControl: some View {
        Menu {
            Picker("Speed", selection: Binding(
                get: { model.settings.rate },
                set: { newValue in Task { await model.perform(.setRate(newValue)) } }
            )) {
                ForEach(PlaybackSettings.rateLadder, id: \.self) { rate in
                    Text(Format.rate(rate)).tag(rate)
                }
            }
        } label: {
            Pill { Text(Format.rate(model.settings.rate)) }
        }
        .accessibilityLabel("Speed")
        .accessibilityValue(Format.rate(model.settings.rate))
    }

    private func command(_ command: PlaybackCommand, systemImage: String, label: String) -> some View {
        Button {
            Task { await model.perform(command) }
        } label: {
            Image(systemName: systemImage)
                .font(.title2)
                .foregroundStyle(Theme.ink)
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// The brand's scrubber: a 6pt surface track, the played portion in the four voices, and a
/// 16pt ink knob ringed in parchment.
///
/// It reports fractions and owns no position: the player view decides what a drag means,
/// which is still "seek on release".
struct ScrubberTrack: View {
    let fraction: Double
    let onDragChanged: (Double?) -> Void
    let onDragEnded: (Double) -> Void

    /// The drag in progress. `@GestureState` rather than `@State` because SwiftUI resets it
    /// when the gesture ends *or is cancelled* — a sheet's pull-down, a system swipe, a call —
    /// where `onEnded` never runs. A plain flag set in `onChanged` stayed set in exactly
    /// those cases and froze the knob and both times where the finger had been.
    @GestureState private var dragFraction: Double?

    private let trackHeight: CGFloat = 6
    private let knobSize: CGFloat = 16

    var body: some View {
        GeometryReader { geometry in
            let width = max(geometry.size.width - knobSize, 1)
            let clamped = min(max(dragFraction ?? fraction, 0), 1)
            let knobX = width * clamped

            ZStack(alignment: .leading) {
                PlayedTrack(fraction: clamped, height: trackHeight)
                    .padding(.horizontal, knobSize / 2)
                Circle()
                    .fill(Theme.parchment)
                    .frame(width: knobSize, height: knobSize)
                    .overlay(Circle().fill(Theme.ink).padding(2.5))
                    .shadow(color: Theme.ink.opacity(0.3), radius: 3, y: 2)
                    .offset(x: knobX)
            }
            .frame(maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .updating($dragFraction) { value, state, _ in
                        state = Self.fraction(at: value.location.x, width: width, inset: knobSize / 2)
                    }
                    .onEnded { value in
                        onDragEnded(Self.fraction(at: value.location.x, width: width, inset: knobSize / 2))
                    }
            )
        }
        .frame(height: 32)
        // Reported rather than owned upstream, so the player's times follow the knob — and a
        // cancelled drag reports nil, which is what puts them back on the playback position.
        .onChange(of: dragFraction) { _, current in
            onDragChanged(current)
        }
    }

    private static func fraction(at x: CGFloat, width: CGFloat, inset: CGFloat) -> Double {
        Double(min(max((x - inset) / width, 0), 1))
    }
}

/// A played portion: the four-voice gradient over a surface track, or ink-mute once there is
/// nothing left to hear. Also what an episode row's progress is drawn with.
struct PlayedTrack: View {
    let fraction: Double
    var height: CGFloat = 6
    var isFinished = false

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.surface)
                if isFinished {
                    Capsule().fill(Theme.inkMute)
                } else {
                    Capsule()
                        .fill(Theme.voiceGradient)
                        .frame(width: geometry.size.width * min(max(fraction, 0), 1))
                }
            }
        }
        .frame(height: height)
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
                        HStack(alignment: .firstTextBaseline) {
                            Text(segment.newsItemTitle)
                                .font(Theme.display(17, relativeTo: .headline))
                                .foregroundStyle(Theme.ink)
                            Spacer()
                            Text(Format.time(segment.startMs))
                                .font(Theme.body(12, weight: 500, relativeTo: .caption))
                                .monospacedDigit()
                                .foregroundStyle(Theme.inkSoft)
                        }
                    }
                    .buttonStyle(.plain)

                    ForEach(Array(segment.claims.enumerated()), id: \.offset) { _, claim in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(claim.text)
                                .font(Theme.body(15, relativeTo: .footnote))
                                .foregroundStyle(Theme.ink)
                            Text("“\(claim.sourceExcerpt)” — \(claim.sourceTitle)")
                                .font(Theme.aside(13, relativeTo: .caption))
                                .foregroundStyle(Theme.inkSoft)
                        }
                    }
                }
                .listRowBackground(isCurrent(segment) ? Theme.surface : Theme.parchment)
                .listRowSeparatorTint(Theme.rule)
            }
        }
        .listStyle(.plain)
        .brandGround()
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
        HStack(spacing: 16) {
            Button(action: onExpand) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(model.playback.currentSegmentTitle ?? model.playback.episodeTitle)
                        .font(Theme.body(15, weight: 500, relativeTo: .subheadline))
                        .foregroundStyle(Theme.ink)
                        .lineLimit(1)
                    // The bar is the surface most often on screen while somebody is
                    // wondering why there is no sound, so it carries the answer rather than
                    // a time that is not moving and a pause button that implies it is.
                    if model.playback.errorMessage != nil {
                        Text(
                            model.playbackProblem?.sentence
                                ?? "Audio could not be loaded. Tap to see why."
                        )
                        .font(Theme.body(12, weight: 500, relativeTo: .caption))
                        .foregroundStyle(Theme.errorText)
                        .lineLimit(1)
                    } else if model.playback.stallMessage != nil {
                        Text("Waiting for audio…")
                            .font(Theme.body(12, weight: 500, relativeTo: .caption))
                            .foregroundStyle(Theme.inkSoft)
                            .lineLimit(1)
                    } else {
                        Text(
                            "\(Format.time(model.playback.positionMs)) · \(Format.rate(model.playback.rate))"
                        )
                        .font(Theme.body(12, weight: 500, relativeTo: .caption))
                        .monospacedDigit()
                        .foregroundStyle(Theme.inkSoft)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .buttonStyle(.plain)

            Button {
                Task { await model.perform(.skipBackward) }
            } label: {
                Image(systemName: "gobackward.15").foregroundStyle(Theme.ink)
            }
            .accessibilityLabel("Back 15 seconds")

            Button {
                Task { await model.perform(.togglePlayPause) }
            } label: {
                Image(systemName: model.playback.isPlaying ? "pause.fill" : "play.fill")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundStyle(Theme.parchment)
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(Theme.ink))
            }
            .accessibilityLabel(model.playback.isPlaying ? "Pause" : "Play")

            Button {
                Task { await model.perform(.skipForward) }
            } label: {
                Image(systemName: "goforward.30").foregroundStyle(Theme.ink)
            }
            .accessibilityLabel("Forward 30 seconds")
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(Theme.surface)
        .overlay(alignment: .top) {
            Rectangle().fill(Theme.rule).frame(height: 1)
        }
    }
}
