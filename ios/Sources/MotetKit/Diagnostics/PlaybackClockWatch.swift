import Foundation

/// **"Is the clock actually moving" as a rule, rather than as a comparison somebody writes
/// at the call site.**
///
/// The naive version — "remember the last position, compare it to this one" — is wrong in
/// both directions on a real player, and each way it is wrong is a failure that already
/// happened here:
///
/// * `AVPlayer` reports its position about once a second. Two samples taken 80 ms apart
///   are *equal* on a perfectly healthy player, so an instantaneous comparison calls
///   working playback frozen.
/// * A player that stops ticking emits nothing at all — no error, no notification, no
///   further position. So "it has not moved" has to be decided by a clock the observer
///   holds, never by waiting for the player to say something.
/// * A seek moves the position without any audio being rendered, and a resume after an
///   interruption can move it backwards.
///
/// So this keeps a short window of (position, time) samples and answers how far the
/// position moved *across the window*. Forward motion only: a backwards jump is a seek or
/// a re-buffer, and neither is evidence that sound came out.
///
/// It is a value type with no dependencies so that the rule is tested on Linux, which is
/// the same argument ``AudioSessionPlan`` makes — the ladder is data in `MotetKit` and the
/// AVFoundation file does nothing but walk it.
public struct PlaybackClockWatch: Sendable {
    /// How far back to look. Long enough to span two of `AVPlayer`'s once-a-second
    /// position reports, so a healthy player is never called frozen, and short enough that
    /// a test does not sit waiting for a verdict.
    public static let window: TimeInterval = 2.5

    private var samples: [(positionMs: Int, at: Date)] = []

    public init() {}

    /// Feed the watch the engine's clock. Call it as often as you like; sampling faster
    /// than the player reports costs nothing but the window's worth of memory.
    public mutating func observe(positionMs: Int, at now: Date) {
        samples.append((positionMs, now))
        let cutoff = now.addingTimeInterval(-Self.window)
        // Keep the newest sample that is at or before the cutoff, so the window is always
        // measured from at least `window` ago rather than from whatever happens to be
        // inside it. Without that, a burst of samples shrinks the window to nothing and
        // the answer becomes the instantaneous comparison this type exists to avoid.
        if let lastStale = samples.lastIndex(where: { $0.at <= cutoff }), lastStale > 0 {
            samples.removeFirst(lastStale)
        }
    }

    /// Throw the window away — after a seek, a load, or anything else that moves the
    /// position without playing it. A seek's echo inside the window would otherwise read
    /// as a second and a half of listening that never happened.
    public mutating func reset() {
        samples.removeAll(keepingCapacity: true)
    }

    /// How far the position moved across the window, never negative.
    ///
    /// Zero until there are two samples: one sample is not a measurement, and reporting
    /// "it has not moved" from it would call every freshly started player frozen.
    public var advancedMs: Int {
        guard let first = samples.first, let last = samples.last, samples.count > 1 else {
            return 0
        }
        return max(0, last.positionMs - first.positionMs)
    }

    /// How long the window currently spans. A caller that wants "frozen for N seconds"
    /// reads this beside ``advancedMs``: zero movement over 0.2 s says nothing, and zero
    /// movement over the full window says the player stopped.
    public var spanSeconds: TimeInterval {
        guard let first = samples.first, let last = samples.last, samples.count > 1 else {
            return 0
        }
        return last.at.timeIntervalSince(first.at)
    }

    /// Whether the window is wide enough for ``advancedMs`` to mean anything.
    public var isConclusive: Bool { spanSeconds >= Self.window * 0.8 }
}
