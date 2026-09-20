import Foundation
import MotetKit

#if canImport(AVFoundation)
import AVFoundation
import os

/// The real audio layer: `AVPlayer`, wrapped so that `PlaybackController` never sees it.
///
/// **The engine reports; it does not decide.** Its clock is published as
/// `PlaybackEngineEvent.position` and the controller does what it likes with it — because
/// `spoken_through_ms` is ours (invariant 4), and `AVPlayer` reports 0 while it re-buffers
/// after an interruption, reports the *item's* time rather than the episode's when the item
/// is replaced, and knows nothing at all after the process is killed.
///
/// Not verifiable in this repo's CI: AVFoundation exists only on Apple platforms, and the
/// behaviours that matter most here — a real interruption from a phone call, route changes
/// when AirPods disconnect, playback continuing with the screen locked — differ between the
/// simulator and a device. See `ios/README.md` for what remains unproven.
public final class AVPlayerPlaybackEngine: PlaybackEngine, PlaybackEngineProbe, @unchecked Sendable {
    private static let logger = Logger(subsystem: "com.getmotet.app", category: "playback")

    private let player = AVPlayer()
    private let lock = NSLock()
    private var handler: (@Sendable (PlaybackEngineEvent) async -> Void)?
    private var timeObserver: Any?
    private var itemObservations: [NSKeyValueObservation] = []
    private var playerObservations: [NSKeyValueObservation] = []
    private var notificationObservers: [NSObjectProtocol] = []
    /// Re-sends the current wait while it lasts. A stalled player's clock does not tick, so
    /// without a heartbeat the controller would see one `waiting` event and never hear
    /// again — and "still waiting twelve seconds later" is the whole thing it has to decide.
    private var waitHeartbeat: DispatchSourceTimer?
    /// Whether the listener has asked for audio. What makes a `.paused` player a fault
    /// rather than a resting one — see ``evaluate()``.
    private var wantsToPlay = false
    /// Whether a wait has actually been reported, so that the heartbeat `play()` arms and
    /// then cancels a second later does not announce the clearing of a wait nobody saw.
    private var reportedWait = false
    /// The episode's own start offset. Always 0 today — one episode is one audio file —
    /// but it is the hook for a future queue where an item is not the whole episode.
    private var itemStartOffsetMs = 0
    /// Measures the audio the mix renders, which is the one observation here that is not
    /// the player's own opinion of itself. See `AudioLevelMeter`.
    private let levelMeter = AudioLevelMeter()
    /// Bumped by every `load`, so a tap that finished attaching after the item it was for
    /// was replaced touches nothing. The attach is off the critical path precisely so that
    /// it can finish late.
    private var itemGeneration = 0
    /// The last failure this engine reported, kept for `engineFacts()`. `AVPlayerItem`
    /// clears nothing on its way out, so without this a probe taken after a failure would
    /// have to reconstruct it from the event stream it is meant to be independent of.
    private var lastErrorMessage: String?

    public init() {
        player.automaticallyWaitsToMinimizeStalling = true
        observeInterruptions()
        observeTimeControlStatus()
    }

    deinit {
        if let timeObserver { player.removeTimeObserver(timeObserver) }
        notificationObservers.forEach(NotificationCenter.default.removeObserver)
        waitHeartbeat?.cancel()
    }

    // MARK: - PlaybackEngine

    public func setEventHandler(_ handler: @escaping @Sendable (PlaybackEngineEvent) async -> Void) async {
        lock.withLock { self.handler = handler }
    }

    public func load(url: URL, startingAtMs: Int) async throws {
        // From an asset rather than a URL, because the audio-level tap needs the asset's
        // audio track and loading it twice would fetch the container header twice.
        let asset = AVURLAsset(url: url)
        let item = AVPlayerItem(asset: asset)
        replaceObservations(for: item)
        let generation = lock.withLock { () -> Int in
            itemGeneration += 1
            lastErrorMessage = nil
            return itemGeneration
        }
        levelMeter.reset()
        player.replaceCurrentItem(with: item)
        itemStartOffsetMs = 0
        if startingAtMs > 0 {
            await player.seek(to: Self.time(fromMs: startingAtMs), toleranceBefore: .zero, toleranceAfter: .zero)
        }
        installTimeObserverIfNeeded()
        attachLevelTap(to: item, asset: asset, generation: generation)
    }

    /// Put the audio-level tap on the item, without making anybody wait for it.
    ///
    /// **Deliberately not awaited.** Loading an asset's tracks reads the container header,
    /// which for a remote episode is a network round trip — and a diagnostic must never be
    /// on the path between pressing play and hearing something. The tap usually lands
    /// before the player is ready anyway, since the player needs that same header; when it
    /// lands later, `audioMix` is assigned to an item that may already be playing, which
    /// AVFoundation supports. When it never lands, `AudioLevelMeter.level()` answers nil
    /// and the probe abstains rather than reporting silence it did not measure.
    private func attachLevelTap(to item: AVPlayerItem, asset: AVAsset, generation: Int) {
        let meter = levelMeter
        let handoff = TapHandoff(item: item, asset: asset)
        Task { [weak self] in
            guard let mix = await meter.audioMix(for: handoff.asset) else { return }
            guard let self, self.lock.withLock({ self.itemGeneration }) == generation else { return }
            handoff.item.audioMix = mix
        }
    }

    /// Carries the item and its asset into the attach task.
    ///
    /// `AVPlayerItem` and `AVAsset` are not `Sendable`, so Swift 6 refuses to let a
    /// `@Sendable` closure capture them — correctly, in general. It is sound here for the
    /// same reason the whole class is `@unchecked Sendable`: exactly one task ever touches
    /// this pair, the generation check above drops it if a newer `load` has happened since,
    /// and assigning `audioMix` is the one thing done with it. Widening what crosses this
    /// boundary is the thing to think twice about.
    private struct TapHandoff: @unchecked Sendable {
        let item: AVPlayerItem
        let asset: AVAsset
    }

    // MARK: - PlaybackEngineProbe

    /// What the player says about itself right now, read live.
    ///
    /// Live rather than folded out of the event stream, because two of these are only
    /// answerable in the moment: `reasonForWaitingToPlay` is nil the instant the wait ends,
    /// and a level is a measurement over a window rather than an event. Reading them here
    /// also keeps the probe independent of whether the controller happened to receive an
    /// event — which matters, since the bug this exists for is a player that emits none.
    public func engineFacts() async -> EngineFacts {
        let status = player.timeControlStatus
        let wantsToPlay = lock.withLock { self.wantsToPlay }
        return EngineFacts(
            transport: Self.transport(status, wantsToPlay: wantsToPlay),
            positionMs: Self.ms(fromTime: player.currentTime()) + itemStartOffsetMs,
            rate: Double(player.rate),
            waitReason: Self.waitReason(status, wantsToPlay: wantsToPlay, player: player),
            level: levelMeter.level(),
            errorMessage: lock.withLock { lastErrorMessage }
        )
    }

    /// The same three-way reading `evaluate()` makes, and it has to stay the same one.
    ///
    /// **`.paused` while the player was told to play is a wait, not a pause** — that is the
    /// case `.notStarted` exists for, and `timeControlStatus` alone cannot see it. A probe
    /// that read the status by itself would report `transport=paused` about a player
    /// somebody pressed Play on, which is the exact reassuring answer this whole mechanism
    /// exists to stop the app giving.
    private static func transport(
        _ status: AVPlayer.TimeControlStatus, wantsToPlay: Bool
    ) -> PlaybackTransport {
        switch status {
        case .playing: return .playing
        case .waitingToPlayAtSpecifiedRate: return .waiting
        case .paused: return wantsToPlay ? .waiting : .paused
        @unknown default: return wantsToPlay ? .waiting : .paused
        }
    }

    private static func waitReason(
        _ status: AVPlayer.TimeControlStatus, wantsToPlay: Bool, player: AVPlayer
    ) -> PlaybackWaitReason? {
        switch status {
        case .waitingToPlayAtSpecifiedRate:
            return reason(player.reasonForWaitingToPlay)
        case .paused:
            return wantsToPlay ? .notStarted : nil
        default:
            return nil
        }
    }

    public func play() async {
        // `playImmediately(atRate:)` rather than `play()`: `play()` resumes at 1.0 and then
        // the rate observer would have to correct it, which is audible.
        lock.withLock { wantsToPlay = true }
        player.playImmediately(atRate: Float(currentRate))
        // Start the watchdog from here rather than waiting for a KVO that may never come:
        // if the command left the player `.paused` — which is what a refused or inactive
        // audio session looks like — `timeControlStatus` does not change, so nothing else
        // in this file would ever fire again. See `evaluate()`.
        startHeartbeat()
        // Deliberately no `emit(.playing)` here. It used to be sent the instant play was
        // *asked for*, which is the claim that was false: on a device the next thing that
        // happens is often `.waitingToPlayAtSpecifiedRate`, and an optimistic `.playing`
        // racing a truthful `.waiting` through two unordered `Task`s would clear the wait
        // the wait had just reported. `timeControlStatus` is now the only source of both,
        // so they cannot disagree.
    }

    public func pause() async {
        lock.withLock { wantsToPlay = false }
        player.pause()
        emit(.paused)
    }

    public func seek(toMs ms: Int) async {
        await player.seek(to: Self.time(fromMs: ms), toleranceBefore: .zero, toleranceAfter: .zero)
        emit(.position(ms: ms))
    }

    public func setRate(_ rate: Double) async {
        currentRate = rate
        // `defaultRate` (iOS 16+) is what a resume after an interruption or a lockscreen
        // play command uses, so setting only `rate` would silently drop back to 1.0.
        player.defaultRate = Float(rate)
        if player.timeControlStatus == .playing {
            player.rate = Float(rate)
        }
    }

    public func currentTimeMs() async -> Int {
        Self.ms(fromTime: player.currentTime()) + itemStartOffsetMs
    }

    // MARK: - Internals

    private var _rate: Double = 1.0
    private var currentRate: Double {
        get { lock.withLock { _rate } }
        set { lock.withLock { _rate = newValue } }
    }

    private func emit(_ event: PlaybackEngineEvent) {
        // Kept for `engineFacts()`: the probe answers without consulting the controller,
        // so the failure has to be readable here too.
        if case .failed(let message) = event {
            lock.withLock { lastErrorMessage = message }
        }
        // AVFoundation's callbacks are synchronous and arrive on a dispatch queue, so the
        // hop into the controller's actor is unavoidable here — and unstructured `Task`s
        // carry NO ordering guarantee, so the controller must not assume events arrive in
        // the order they were emitted. It does not: a position report that arrives after
        // the seek it preceded reads as a backwards jump, which `PlaybackController`
        // refuses to count as listening rather than trusting.
        guard let handler = lock.withLock({ self.handler }) else { return }
        Task { await handler(event) }
    }

    private func installTimeObserverIfNeeded() {
        guard timeObserver == nil else { return }
        // Once a second: enough for a progress bar and for segment boundaries (segments are
        // tens of seconds), and cheap enough to leave running with the screen off.
        let interval = CMTime(seconds: 1, preferredTimescale: CMTimeScale(NSEC_PER_SEC))
        timeObserver = player.addPeriodicTimeObserver(forInterval: interval, queue: .main) { [weak self] time in
            guard let self else { return }
            self.emit(.position(ms: Self.ms(fromTime: time) + self.itemStartOffsetMs))
        }
    }

    /// The observation whose absence was the bug.
    ///
    /// `AVPlayer` has three control states and this engine only ever reported two of them.
    /// `.playing` and `.paused` were wired; `.waitingToPlayAtSpecifiedRate` was not — and
    /// that is the one a real device sits in when a remote asset will not start. In it the
    /// clock does not advance, no item error is ever set, `didPlayToEndTime` never fires,
    /// and `AVPlayerItem.status` stays `.readyToPlay`, so every other observation in this
    /// file reports that everything is fine. The listener sees a pause button over silence.
    ///
    /// `reasonForWaitingToPlay` is the player's own account of why, and it is only readable
    /// while the wait is on, so it is read here rather than reconstructed later.
    private func observeTimeControlStatus() {
        playerObservations.append(
            player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] _, _ in
                self?.evaluate()
            }
        )
    }

    /// One reading of the player, from both the KVO and the heartbeat.
    ///
    /// **`.paused` is the case worth spelling out.** `playImmediately(atRate:)` can leave
    /// the player there — a refused or inactive audio session looks exactly like that from
    /// here — and then `timeControlStatus` never changes again, so no KVO ever fires, no
    /// `reasonForWaitingToPlay` exists to read, and without this branch the controller sits
    /// at "playing" over a clock that will never move. That is the reported bug, and it is
    /// why the heartbeat is armed by `play()` rather than only by a wait.
    private func evaluate() {
        switch player.timeControlStatus {
        case .playing:
            endWaiting()
            emit(.playing)
        case .waitingToPlayAtSpecifiedRate:
            beginWaiting(Self.reason(player.reasonForWaitingToPlay))
        case .paused:
            if lock.withLock({ wantsToPlay }) {
                beginWaiting(.notStarted)
            } else {
                endWaiting()
            }
        @unknown default:
            endWaiting()
        }
    }

    /// `AVPlayer.WaitingReason` is a `String`-backed struct rather than an enum, so a
    /// release can add a reason without this file changing — which is what `.unknown` is
    /// for. A reason nobody here recognises is still reported: an unnamed wait is exactly
    /// the kind that never gets looked at.
    private static func reason(_ reason: AVPlayer.WaitingReason?) -> PlaybackWaitReason {
        guard let reason else { return .unknown }
        switch reason {
        case .noItemToPlay: return .noItemToPlay
        case .toMinimizeStalls: return .toMinimizeStalls
        case .evaluatingBufferingRate: return .evaluatingBufferingRate
        case .interstitialEvent: return .interstitialEvent
        case .waitingForCoordinatedPlayback: return .waitingForCoordinatedPlayback
        default: return .unknown
        }
    }

    private func beginWaiting(_ reason: PlaybackWaitReason) {
        let first = lock.withLock { () -> Bool in
            defer { reportedWait = true }
            return !reportedWait
        }
        if first {
            // At `notice` rather than `debug`: this is the line that travels off somebody
            // else's phone in a sysdiagnose, and it is the whole answer to "Play did
            // nothing". The screen says the same thing, but only while they are looking.
            Self.logger.notice("playback waiting: \(reason.rawValue, privacy: .public)")
        }
        emit(.waiting(reason))
        startHeartbeat()
    }

    /// Arm the re-evaluation timer if it is not already running.
    ///
    /// Idempotent, and deliberately not restarted on a reason change: the controller's
    /// grace period is measured from the start of the *wait*, and a timer restarted every
    /// time `AVPlayer` walks from `evaluatingBufferingRate` to `toMinimizeStalls` would
    /// keep resetting the thing that is supposed to be bounding it.
    private func startHeartbeat() {
        let alreadyRunning = lock.withLock { () -> Bool in
            guard waitHeartbeat == nil else { return true }
            return false
        }
        guard !alreadyRunning else { return }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        let interval = PlaybackController.stallProbeSeconds
        timer.schedule(deadline: .now() + interval, repeating: interval)
        timer.setEventHandler { [weak self] in self?.evaluate() }
        lock.withLock { waitHeartbeat = timer }
        timer.resume()
    }

    /// Nobody is waiting for audio any more.
    ///
    /// Without this the `.notStarted` watchdog outlives what it was watching: `AVPlayer`
    /// returns to `.paused` when an item ends, fails, or is interrupted, and `wantsToPlay`
    /// would still be true — so `evaluate()` would call that a stall and re-arm the
    /// heartbeat every two seconds for the rest of the process. The controller hides it
    /// (a wait is only narrated while it thinks it is playing), which is exactly why it
    /// would never have been noticed.
    private func stopWanting() {
        lock.withLock { wantsToPlay = false }
        endWaiting()
    }

    private func endWaiting() {
        let (timer, wasWaiting) = lock.withLock { () -> (DispatchSourceTimer?, Bool) in
            defer {
                waitHeartbeat = nil
                reportedWait = false
            }
            return (waitHeartbeat, reportedWait)
        }
        timer?.cancel()
        guard wasWaiting else { return }
        Self.logger.notice("playback waiting: cleared")
        emit(.waiting(nil))
    }

    private func replaceObservations(for item: AVPlayerItem) {
        itemObservations.removeAll()
        notificationObservers.forEach(NotificationCenter.default.removeObserver)
        notificationObservers.removeAll()
        observeInterruptions()

        itemObservations.append(item.observe(\.status, options: [.new]) { [weak self] item, _ in
            guard let self else { return }
            switch item.status {
            case .readyToPlay:
                self.emit(.ready(durationMs: Self.ms(fromTime: item.duration)))
            case .failed:
                self.stopWanting()
                self.emit(.failed(item.error.map { String(describing: $0) } ?? "playback failed"))
            default:
                break
            }
        })

        itemObservations.append(item.observe(\.isPlaybackLikelyToKeepUp, options: [.new]) { [weak self] item, _ in
            if !item.isPlaybackLikelyToKeepUp { self?.emit(.stalled) }
        })

        notificationObservers.append(
            NotificationCenter.default.addObserver(
                forName: AVPlayerItem.didPlayToEndTimeNotification, object: item, queue: .main
            ) { [weak self] _ in
                self?.stopWanting()
                self?.emit(.ended)
            }
        )
        notificationObservers.append(
            NotificationCenter.default.addObserver(
                forName: AVPlayerItem.failedToPlayToEndTimeNotification, object: item, queue: .main
            ) { [weak self] note in
                let error = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
                self?.stopWanting()
                self?.emit(.failed(error.map { String(describing: $0) } ?? "playback failed"))
            }
        )
    }

    /// A phone call, Siri, or another app taking the session.
    ///
    /// `.shouldResume` in the option set is iOS saying it is polite to start again; anything
    /// else means stay stopped. The controller keeps the position either way.
    private func observeInterruptions() {
        #if canImport(UIKit) && !os(watchOS)
        notificationObservers.append(
            NotificationCenter.default.addObserver(
                forName: AVAudioSession.interruptionNotification,
                object: AVAudioSession.sharedInstance(),
                queue: .main
            ) { [weak self] note in
                guard let self,
                      let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                      let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
                switch type {
                case .began:
                    // The system took the audio; nobody is waiting on us. A resume comes
                    // back through `play()`, which arms the watchdog again.
                    self.stopWanting()
                    self.emit(.interrupted(resumable: true))
                case .ended:
                    let options = (note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt)
                        .map(AVAudioSession.InterruptionOptions.init(rawValue:)) ?? []
                    self.emit(.interrupted(resumable: options.contains(.shouldResume)))
                @unknown default:
                    break
                }
            }
        )
        #endif
    }

    private static func time(fromMs ms: Int) -> CMTime {
        CMTime(value: CMTimeValue(max(0, ms)), timescale: 1_000)
    }

    private static func ms(fromTime time: CMTime) -> Int {
        guard time.isValid, !time.isIndefinite, time.seconds.isFinite else { return 0 }
        return Int((time.seconds * 1_000).rounded())
    }
}
#endif
