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
    private var notificationObservers: [NSObjectProtocol] = []
    /// The episode's own start offset. Always 0 today — one episode is one audio file —
    /// but it is the hook for a future queue where an item is not the whole episode.
    private var itemStartOffsetMs = 0

    public init() {
        player.automaticallyWaitsToMinimizeStalling = true
        observeInterruptions()
    }

    deinit {
        if let timeObserver { player.removeTimeObserver(timeObserver) }
        notificationObservers.forEach(NotificationCenter.default.removeObserver)
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
        emit(.playing)
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
