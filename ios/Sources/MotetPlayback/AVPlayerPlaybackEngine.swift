import Foundation
import MotetKit

#if canImport(AVFoundation)
import AVFoundation

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
public final class AVPlayerPlaybackEngine: PlaybackEngine, @unchecked Sendable {
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
    /// The episode's own start offset. Always 0 today — one episode is one audio file —
    /// but it is the hook for a future queue where an item is not the whole episode.
    private var itemStartOffsetMs = 0

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
        let item = AVPlayerItem(url: url)
        replaceObservations(for: item)
        player.replaceCurrentItem(with: item)
        itemStartOffsetMs = 0
        if startingAtMs > 0 {
            await player.seek(to: Self.time(fromMs: startingAtMs), toleranceBefore: .zero, toleranceAfter: .zero)
        }
        installTimeObserverIfNeeded()
    }

    public func play() async {
        // `playImmediately(atRate:)` rather than `play()`: `play()` resumes at 1.0 and then
        // the rate observer would have to correct it, which is audible.
        player.playImmediately(atRate: Float(currentRate))
        // Deliberately no `emit(.playing)` here. It used to be sent the instant play was
        // *asked for*, which is the claim that was false: on a device the next thing that
        // happens is often `.waitingToPlayAtSpecifiedRate`, and an optimistic `.playing`
        // racing a truthful `.waiting` through two unordered `Task`s would clear the wait
        // the wait had just reported. `timeControlStatus` is now the only source of both,
        // so they cannot disagree.
    }

    public func pause() async {
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
            player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] player, _ in
                guard let self else { return }
                switch player.timeControlStatus {
                case .waitingToPlayAtSpecifiedRate:
                    self.beginWaiting(Self.reason(player.reasonForWaitingToPlay))
                case .playing:
                    self.endWaiting()
                    self.emit(.playing)
                case .paused:
                    self.endWaiting()
                @unknown default:
                    self.endWaiting()
                }
            }
        )
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
        emit(.waiting(reason))
        // One heartbeat for the whole wait, replaced rather than stacked: `timeControlStatus`
        // can report a *different* reason without leaving the waiting state, and two timers
        // would both survive the one `endWaiting` that follows.
        let existing = lock.withLock { () -> DispatchSourceTimer? in
            defer { waitHeartbeat = nil }
            return waitHeartbeat
        }
        existing?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        let interval = PlaybackController.stallProbeSeconds
        timer.schedule(deadline: .now() + interval, repeating: interval)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            // Read the live reason each tick rather than the captured one: a wait that
            // started as "measuring the connection" and became "nothing to play" is a
            // different answer, and the second is the one worth reporting.
            guard self.player.timeControlStatus == .waitingToPlayAtSpecifiedRate else {
                self.endWaiting()
                return
            }
            self.emit(.waiting(Self.reason(self.player.reasonForWaitingToPlay)))
        }
        lock.withLock { waitHeartbeat = timer }
        timer.resume()
    }

    private func endWaiting() {
        let timer = lock.withLock { () -> DispatchSourceTimer? in
            defer { waitHeartbeat = nil }
            return waitHeartbeat
        }
        guard timer != nil else { return }
        timer?.cancel()
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
                self?.emit(.ended)
            }
        )
        notificationObservers.append(
            NotificationCenter.default.addObserver(
                forName: AVPlayerItem.failedToPlayToEndTimeNotification, object: item, queue: .main
            ) { [weak self] note in
                let error = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
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
