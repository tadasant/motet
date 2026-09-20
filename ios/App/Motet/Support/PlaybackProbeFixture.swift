#if DEBUG
import Foundation
import MotetKit
import MotetPlayback
import SwiftUI

/// **A Debug-only screen that plays a locally generated tone through the real player, so
/// that an automated run can prove the playback signal moves.**
///
/// It exists because of what the alternatives cannot do. **No cloud device service
/// captures iOS audio** — AWS Device Farm's session artifacts are video, logs and
/// screenshots with no audio track, and Appetize deprecated iOS audio output and never
/// supported microphone input — so "is sound coming out" has to be answered by the app
/// about itself. And **an agent cannot sign in**: Google refuses an automated browser at
/// the identifier step (AGENTS.md, "An agent cannot sign in"), so a UI test can reach no
/// screen that needs a session and no episode that comes from a server.
///
/// What is left is this: the real `AVPlayerPlaybackEngine`, the real
/// `AudioSessionController`, the real `PlaybackController` and the real
/// `PlaybackProbeRecorder`, pointed at a file this process wrote a second ago. Everything
/// between the play button and the audio tap is the code that ships. What it deliberately
/// does *not* exercise is the network half — the feed token, the signed URL, the 307 — and
/// that is a separate claim with its own tests.
///
/// The tone is generated rather than committed: a WAV is a header and some arithmetic, and
/// a binary fixture in the repository is a thing nobody can review.
enum PlaybackProbeFixture {
    /// `-MotetPlaybackProbe` on the launch arguments. Same shape as `ScreenshotFixture`,
    /// and for the same reason — a simulator has to be driveable without a server.
    static var isRequested: Bool {
        ProcessInfo.processInfo.arguments.contains("-MotetPlaybackProbe")
    }

    /// `-MotetPlaybackProbeAutoplay` alongside it: start playing as soon as the tone is
    /// loaded, with nobody to press the button.
    ///
    /// It exists because `xcrun simctl` can take a screenshot and cannot tap anything, and
    /// a picture of the probe reading `SILENT: notPlaying` proves nothing worth having. The
    /// UI test deliberately does **not** use it — its whole assertion is the transition,
    /// which needs a press.
    static var autoplays: Bool {
        ProcessInfo.processInfo.arguments.contains("-MotetPlaybackProbeAutoplay")
    }

    static let durationMs = 20_000
    static let episodeId = "probe-tone"

    /// An episode with no segments, deliberately: read state is per news item and this has
    /// none, so nothing is marked read and no write is attempted against a server that is
    /// not there.
    static var episode: EpisodeResponse {
        EpisodeResponse(
            audioMediaType: "audio/wav",
            createdAt: Date(timeIntervalSince1970: 1_800_000_000),
            durationMs: durationMs,
            id: episodeId,
            listenedThroughMs: 0,
            maxDurationMs: durationMs,
            segments: [],
            state: "ready",
            title: "Playback probe"
        )
    }

    /// Write the tone and answer where it went.
    ///
    /// 440 Hz at half scale, which is loud enough that `AudioLevel.isSounding`'s -66 dBFS
    /// floor is not a close call, and continuous, so a measurement taken at any moment
    /// during playback is the same measurement.
    static func writeTone() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("motet-probe-tone.wav")
        let tone = wav(seconds: Double(durationMs) / 1_000)
        // Length as well as existence: a run killed mid-write leaves a truncated file that
        // existence alone would reuse forever on a simulator nobody erases.
        let onDisk = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? Int
        if onDisk != tone.count {
            try tone.write(to: url, options: .atomic)
        }
        return url
    }

    /// A 16-bit mono PCM WAV. Hand-rolled because it is forty lines of header and a sine,
    /// and because the alternative is a binary blob in a public repository that no review
    /// can read.
    static func wav(seconds: Double, sampleRate: Int = 22_050, hz: Double = 440) -> Data {
        let frames = Int(Double(sampleRate) * seconds)
        var samples = Data(capacity: frames * 2)
        for frame in 0..<frames {
            let value = sin(2 * Double.pi * hz * Double(frame) / Double(sampleRate)) * 0.5
            let scaled = Int16(max(-32_767, min(32_767, value * 32_767)))
            withUnsafeBytes(of: scaled.littleEndian) { samples.append(contentsOf: $0) }
        }
        var out = Data()
        func ascii(_ text: String) { out.append(contentsOf: Array(text.utf8)) }
        func u32(_ value: UInt32) { withUnsafeBytes(of: value.littleEndian) { out.append(contentsOf: $0) } }
        func u16(_ value: UInt16) { withUnsafeBytes(of: value.littleEndian) { out.append(contentsOf: $0) } }
        ascii("RIFF")
        u32(UInt32(36 + samples.count))
        ascii("WAVE")
        ascii("fmt ")
        u32(16)                                   // PCM header length
        u16(1)                                    // PCM, uncompressed
        u16(1)                                    // mono
        u32(UInt32(sampleRate))
        u32(UInt32(sampleRate * 2))               // byte rate
        u16(2)                                    // block align
        u16(16)                                   // bits per sample
        ascii("data")
        u32(UInt32(samples.count))
        out.append(samples)
        return out
    }
}

/// The screen. Everything on it carries an accessibility identifier, because its only two
/// readers are a UI test and whoever is looking at the video that test recorded.
struct PlaybackProbeView: View {
    @EnvironmentObject private var model: AppModel
    @State private var loadError: String?
    @State private var isReady = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Playback probe")
                .font(Theme.display(22, relativeTo: .title3))
                .accessibilityIdentifier("probe-title")

            Text(model.buildTarget.summary)
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(Theme.inkSoft)
                .accessibilityIdentifier("build-target")

            if let loadError {
                Text(loadError)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(Theme.errorText)
                    .accessibilityIdentifier("probe-load-error")
            }

            HStack(spacing: 12) {
                Button(model.playback.isPlaying ? "Pause" : "Play") {
                    Task { await model.perform(.togglePlayPause) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!isReady)
                .accessibilityIdentifier("probe-play-pause")

                Text(isReady ? "loaded" : "loading")
                    .font(.system(.caption, design: .monospaced))
                    .accessibilityIdentifier("probe-load-state")
            }

            PlaybackProbeStrip(probe: model.playbackProbe)

            Spacer()
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Theme.parchment.ignoresSafeArea())
        .task { await load() }
    }

    @MainActor
    private func load() async {
        guard !isReady, loadError == nil else { return }
        let environment = AppEnvironment.shared
        await environment.activate()
        // Both, and the second is easy to forget: `MotetApp` skips `model.start()` for this
        // fixture, so without it nothing subscribes to the controller's snapshots — the
        // button below would read "Play" while audio was playing, and the probe reporter,
        // which only logs about an episode it knows the id of, would stay silent for the
        // whole run. That is the one place this repo actually runs the app.
        model.observePlayback()
        model.observeSnapshots()
        await environment.applyAudioSessionShape()
        // Reported, never fatal: a simulator that refuses the session is a finding the
        // probe should show rather than a reason to render nothing.
        do { try environment.audioSession.activate() } catch {
            loadError = "session: \(error.localizedDescription)"
        }
        do {
            let url = try PlaybackProbeFixture.writeTone()
            try await model.controller.load(
                episode: PlaybackProbeFixture.episode,
                source: PlaybackController.Source(url: url, isLocal: true),
                autoplay: false
            )
            isReady = true
            if PlaybackProbeFixture.autoplays {
                await model.perform(.play)
            }
        } catch {
            loadError = "load: \(String(describing: error))"
        }
    }
}

/// The probe, drawn. One identifier per field, plus the whole line under
/// `playback-probe` — a test asserts on the parsed line, and a human reads the fields.
struct PlaybackProbeStrip: View {
    let probe: PlaybackProbe

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(probe.isAudible ? "AUDIBLE" : "SILENT: \(probe.silenceReason?.rawValue ?? "-")")
                .font(.system(.footnote, design: .monospaced).weight(.bold))
                .foregroundStyle(probe.isAudible ? Theme.ink : Theme.errorText)
                .accessibilityIdentifier("probe-verdict")
            Text(probe.summary)
                .font(.system(size: 10, design: .monospaced))
                .foregroundStyle(Theme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("playback-probe")
        }
        .padding(8)
        .background(Theme.ink.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
    }
}
#endif
