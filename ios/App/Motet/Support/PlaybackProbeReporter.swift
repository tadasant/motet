import Foundation
import MotetKit
import os

/// Polls ``PlaybackProbeRecorder`` and puts its answer where three different readers can
/// get at it: the screen, the log, and a UI test.
///
/// **This exists because the only witness to "there was no sound" was a person.** No cloud
/// device service captures iOS audio, so the one report this app ever got about silent
/// playback — *"The Play button doesn't actually start the audio… the timer line doesn't
/// move"* — could not be reproduced by anything automated, and nothing in the app
/// contradicted it either: `AVPlayer` sat in `.waitingToPlayAtSpecifiedRate`, which sets no
/// error and emits no further event. A probe that nobody reads is the same silence one
/// layer in, so this is the reader.
///
/// Three properties are the design:
///
/// * **It runs in every configuration, not only Debug.** The overlay is Debug-only because
///   it is furniture; the *log line* is what makes a TestFlight build answer the question,
///   and it is the only thing that would have caught this bug where it happened.
/// * **It logs on change, and on a slow heartbeat otherwise.** A line per sample would
///   bury the one that matters; a line only on change would leave a walk that was fine
///   with no evidence that it was.
/// * **It never speaks for a layer it does not have.** `PlaybackProbe.isAudible` abstains
///   where nothing measured the audio, and this reports that verdict rather than
///   second-guessing it.
@MainActor
final class PlaybackProbeReporter {
    private static let logger = Logger(subsystem: "com.getmotet.app", category: "playback-probe")

    /// The most recent answer.
    private(set) var probe = PlaybackProbe()

    /// Where each answer goes. `AppModel` sets this and republishes; a plain closure
    /// rather than a publisher because both ends are already on the main actor and an
    /// `AsyncPublisher` across that boundary buys nothing but a Sendable argument.
    var onProbe: ((PlaybackProbe) -> Void)?

    /// How often to sample while something is meant to be playing. Twice a second: the
    /// player reports its own clock about once a second, and a UI test that has to wait
    /// out ``PlaybackClockWatch/window`` should not also wait out the polling.
    static let activeInterval: TimeInterval = 0.5
    /// And while nothing is. Cheap, and still enough to notice a play that starts.
    static let idleInterval: TimeInterval = 2.0
    /// The heartbeat: how long the same verdict goes unlogged before it is said again.
    static let heartbeatSeconds: TimeInterval = 5

    private let recorder: PlaybackProbeRecorder
    private var task: Task<Void, Never>?
    private var lastLoggedAt: Date?
    private var lastLoggedVerdict: Bool?

    init(recorder: PlaybackProbeRecorder) {
        self.recorder = recorder
    }

    /// No `deinit` cancelling the task, deliberately: the loop below holds `self` weakly
    /// and returns the moment this object goes, and a `deinit` on a `@MainActor` type
    /// reaching isolated state is a hazard Swift 6 is right to complain about.
    ///
    /// Start sampling. Idempotent — a second call replaces the first rather than running
    /// two loops, which is what a `reconfigure()` would otherwise do.
    func start() {
        task?.cancel()
        task = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let probe = await self.recorder.sample()
                self.publish(probe)
                let interval = probe.transport == .paused
                    ? Self.idleInterval : Self.activeInterval
                try? await Task.sleep(for: .seconds(interval))
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
    }

    /// The episode the probe's lines are about.
    func track(episodeId: String?) {
        Task { await recorder.track(episodeId: episodeId) }
    }

    /// A seek, a load, or a resume: the position moved without being played, so the window
    /// that judges "is the clock advancing" has to be thrown away rather than reading the
    /// jump as a second of listening.
    func noteDiscontinuity() {
        Task { await recorder.discontinuity() }
    }

    private func publish(_ probe: PlaybackProbe) {
        self.probe = probe
        onProbe?(probe)
        log(probe)
    }

    private func log(_ probe: PlaybackProbe) {
        let now = Date()
        let verdictChanged = lastLoggedVerdict != probe.isAudible
        let heartbeatDue = lastLoggedAt.map { now.timeIntervalSince($0) >= Self.heartbeatSeconds }
            ?? true
        // A paused player with nothing loaded is not news, and a line every two seconds
        // for the life of the process is how a log stops being read.
        let worthSaying = probe.episodeId != nil && (verdictChanged || heartbeatDue)
        guard worthSaying else { return }
        lastLoggedAt = now
        lastLoggedVerdict = probe.isAudible
        let line = probe.summary
        let episode = probe.episodeId ?? "none"
        // WARNING for the case somebody is chasing: told to play and producing nothing.
        // Never ERROR — that channel pages, and a phone with the ringer switch on is not
        // an incident.
        if probe.transport != .paused, !probe.isAudible {
            Self.logger.warning("playback silent: episode=\(episode, privacy: .public) \(line, privacy: .public)")
        } else {
            Self.logger.notice("playback: episode=\(episode, privacy: .public) \(line, privacy: .public)")
        }
    }
}
