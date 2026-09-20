import MotetKit
import SwiftUI

/// The brand's mic pill, and Play Live's primary control — the SPA's, press for press: it
/// starts Play Live, and while narrating it interrupts (`barge_in`). Nothing about it is
/// hold-to-talk; that interaction exists on neither client (AGENTS.md, motet#110).
struct LivePill: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        let live = model.live
        Button {
            Task {
                switch live.phase {
                case .idle, .error: await model.startLive()
                case .narrating, .paused: await model.interruptLive()
                default: break
                }
            }
        } label: {
            Pill(filled: true) {
                Image(systemName: live.isRunning ? "mic.fill" : "mic")
                    .font(.system(size: 12, weight: .semibold))
                Text(title(live))
            }
        }
        .buttonStyle(.plain)
        .disabled(!isEnabled(live))
        .opacity(isEnabled(live) ? 1 : 0.42)
        .accessibilityLabel(live.phase == .narrating || live.phase == .paused ? "Just ask (interrupt)" : title(live))
    }

    private func title(_ live: LiveSnapshot) -> String {
        switch live.phase {
        case .idle, .error: return "Play Live"
        case .narrating, .paused: return "just ask"
        case .connecting: return "connecting…"
        case .replying: return "replying…"
        case .resuming: return "resuming…"
        case .listening: return "listening…"
        }
    }

    private func isEnabled(_ live: LiveSnapshot) -> Bool {
        guard live.availability == .available, model.playback.episodeId != nil else { return false }
        switch live.phase {
        case .idle, .error, .narrating, .paused: return true
        default: return false
        }
    }
}

/// Everything Play Live says while it runs: what it is doing, the mic, the question box when
/// it is listening, and the conversation so far.
struct LivePanel: View {
    @EnvironmentObject private var model: AppModel
    @State private var question = ""
    @FocusState private var questionFocused: Bool

    var body: some View {
        let live = model.live
        if live.isRunning || live.error != nil {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    Text(live.phaseLabel)
                        .font(Theme.body(14, weight: 600, relativeTo: .footnote))
                        .foregroundStyle(live.phase == .error ? Theme.errorText : Theme.ink)
                    Spacer()
                    if live.isRunning {
                        Button("Stop Live") { Task { await model.stopLive() } }
                            .font(Theme.body(14, weight: 600))
                    }
                }
                if live.isRunning {
                    HStack(spacing: 8) {
                        MicMeter(dbfs: live.micDbfs)
                        Text("\(Int(live.micDbfs.rounded())) dBFS\(live.arm.isEmpty ? "" : " · \(live.arm)")")
                            .font(Theme.body(11, weight: 500, relativeTo: .caption2))
                            .monospacedDigit()
                            .foregroundStyle(Theme.inkSoft)
                    }
                }
                if let error = live.error {
                    Text(error)
                        .font(Theme.body(13, relativeTo: .footnote))
                        .foregroundStyle(Theme.errorText)
                }
                // Two different situations, and the wrong sentence is worse than none: a
                // live channel that did not open still answers a typed question, while an
                // arm that cannot converse answers nothing at all — which is what production
                // has been doing while looking fine. The service says which
                // (`can_answer`); the code alone cannot, because `arm_dormant` is emitted
                // for both.
                if live.isRunning, !live.canAnswer {
                    Text(
                        "Play Live can't answer in this deployment, so you will hear nothing back. "
                            + (live.liveUnavailableDetail ?? "No conversational vendor is provisioned.")
                    )
                    .font(Theme.body(12, relativeTo: .caption))
                    .foregroundStyle(Theme.errorText)
                } else if live.isRunning, let reason = live.liveUnavailable {
                    Text("Live conversation unavailable (\(reason)); typed questions are answered instead.")
                        .font(Theme.body(12, relativeTo: .caption))
                        .foregroundStyle(Theme.inkSoft)
                }
                if live.phase == .listening {
                    HStack(spacing: 8) {
                        TextField(live.isLive ? "or type your question" : "Your question", text: $question)
                            .textFieldStyle(.roundedBorder)
                            .focused($questionFocused)
                            .submitLabel(.send)
                            .onSubmit(ask)
                        Button("Ask", action: ask)
                            .font(Theme.body(14, weight: 600))
                            .disabled(question.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    Button("Never mind, resume") { Task { await model.resumeLiveNarration() } }
                        .font(Theme.body(13, weight: 500))
                }
                if live.isRunning, let turnError = live.turnError {
                    Text("That reply failed — \(turnError)")
                        .font(Theme.body(12, relativeTo: .caption))
                        .foregroundStyle(Theme.errorText)
                }
                if let interrupted = live.lastInterrupt {
                    Text("Interrupted during: \(interrupted)")
                        .font(Theme.aside(13))
                        .foregroundStyle(Theme.inkSoft)
                        .lineLimit(2)
                }
                ForEach(live.lines.suffix(4)) { line in
                    LiveLineView(line: line, isLive: live.isLive)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .brandCard(fill: Theme.surface)
        }
    }

    private func ask() {
        let text = question
        question = ""
        Task { await model.askLive(text) }
    }
}

private struct LiveLineView: View {
    let line: LiveSnapshot.Line
    let isLive: Bool

    var body: some View {
        switch line.kind {
        case .user:
            Text("**You\(isLive ? " (heard)" : ""):** \(line.text)")
                .font(Theme.body(13, relativeTo: .footnote))
        case .assistant:
            Text("**Motet:** \(line.text)")
                .font(Theme.body(13, relativeTo: .footnote))
        case .tool, .event:
            Text(line.text)
                .font(Theme.body(11, relativeTo: .caption2))
                .foregroundStyle(Theme.inkSoft)
                .lineLimit(2)
        }
    }
}

/// The mic level, so "it can't hear me" and "it hears everything" are visible: a mic that
/// shows -65 between words and -25 while talking is one the detector can hear.
private struct MicMeter: View {
    let dbfs: Double

    var body: some View {
        let fraction = min(1, max(0, (dbfs + 70) / 60))
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.parchment)
                Capsule().fill(Theme.ink.opacity(0.66)).frame(width: geometry.size.width * fraction)
            }
        }
        .frame(width: 80, height: 5)
        .accessibilityLabel("Mic level")
        .accessibilityValue("\(Int(dbfs.rounded())) dBFS")
    }
}
