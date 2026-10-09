import Foundation

/// **The playback probe, reduced to what a compressed screen recording can carry** (motet#152).
///
/// ``PlaybackProbe`` answers "is sound coming out" for a log line and a UI test. A cloud
/// device farm can do neither: its session artifacts are a video with no audio track, and
/// the only other thing its driver can read is the accessibility tree. So this is the same
/// measurement as a handful of fixed fields — each one a short token large enough to read
/// off a farm video frame, and each one the same token an automated driver reads off the
/// element's accessibility value.
///
/// It differs from ``PlaybackProbe/isAudible`` in one deliberate way, and in the direction
/// that can only make it say less:
///
/// * **`audible` requires layer 3.** `isAudible` treats an absent level as an abstention
///   and lets layers 1 and 2 decide, which is right for a log line about a player that has
///   never been measured. A readout whose whole job is to stand in for a pair of ears must
///   not say `audible` about audio nothing heard, so where the tap measured nothing this
///   says ``Verdict/unmeasured`` — still not `silent`, because #140's rule holds: a missing
///   layer abstains, it never votes no.
///
/// **Live mode's replies are not measured, and the readout says so rather than guessing.**
/// The layer-3 tap sits on `AVPlayer`'s audio mix, and a Play Live reply is played by a
/// separate `AVAudioEngine` (`AVLiveAudio`). So in Live mode the narration is measured as
/// it always was, and only it can produce `audible`. While the session is narrating and the
/// player is playing, the narration's layers decide exactly as in Listen mode — a frozen
/// clock there is still `silent`. Every other moment of a Live session — connecting, the
/// listener asking, a reply playing, narration paused — is ``Verdict/unmeasured``, because
/// the path that might be making sound is one nothing is listening to.
///
/// A value type with no dependencies, for ``PlaybackClockWatch``'s reason: the rules are
/// tested on Linux, and the SwiftUI view that draws them does nothing but draw.
public struct AudioReadout: Hashable, Sendable {
    public enum Mode: String, Hashable, Sendable, CaseIterable {
        /// The narration, through `AVPlayer`. Every layer is available.
        case listen
        /// A Play Live session is running. The narration is still measured; the replies are
        /// not.
        case live
    }

    public enum Verdict: String, Hashable, Sendable, CaseIterable {
        /// Every layer agrees, and layer 3 measured sound.
        case audible
        /// A layer that was measured says nothing is coming out.
        case silent
        /// Nothing measured the path that could be making sound. Never a vote either way.
        case unmeasured
    }

    /// Whether the position moved over ``PlaybackClockWatch``'s window.
    public enum Clock: String, Hashable, Sendable, CaseIterable {
        /// The transport is running and the position moved.
        case advancing
        /// The transport says it is running and the position did not move — the failure
        /// that started #140.
        case frozen
        /// The transport is not running, so there is no clock to judge.
        case stopped
    }

    public var mode: Mode
    public var verdict: Verdict
    /// Why the verdict is not `audible`, as a short token; `none` when it is.
    public var reason: String
    public var transport: PlaybackTransport
    public var positionMs: Int
    public var clock: Clock
    /// Layer 3's RMS, or nil where nothing measured it.
    public var rms: Double?
    /// Layer 3's peak, or nil where nothing measured it.
    public var peak: Double?
    /// The current route's outputs joined with `+`, `none` for an empty route, `unknown`
    /// where the session could not be asked.
    public var route: String
    /// Play Live's phase while a session runs, nil in Listen mode.
    public var livePhase: String?

    public init(probe: PlaybackProbe, live: LiveSnapshot? = nil) {
        let isLive = live?.isRunning ?? false
        mode = isLive ? .live : .listen
        livePhase = isLive ? live?.phase.rawValue : nil
        transport = probe.transport
        positionMs = probe.positionMs
        if probe.transport != .playing {
            clock = .stopped
        } else {
            clock = probe.advancedMs > 0 ? .advancing : .frozen
        }
        rms = probe.level?.rms
        peak = probe.level?.peak
        if let routeFacts = probe.route {
            route = routeFacts.outputs.isEmpty ? "none" : routeFacts.outputs.joined(separator: "+")
        } else {
            route = "unknown"
        }

        let narration = Self.narrationVerdict(probe)
        let phase = live?.phase
        if mode == .listen || narration.verdict == .audible
            || (phase == .narrating && probe.transport == .playing) {
            // Listen mode; or a measured narration; or a Live session that says the narration
            // is the thing sounding — replies are flushed before it narrates again — and the
            // player agrees. In each the narration's own layers are the whole question, so a
            // frozen clock or a measured hush under a narrating session still reads `silent`.
            verdict = narration.verdict
            reason = narration.reason
        } else {
            // The narration is not what should be sounding, so whatever is — a reply, or
            // nothing — came through a path no tap is on. Never a vote either way.
            verdict = .unmeasured
            switch phase {
            case .listening?, .replying?, .resuming?:
                reason = "live_reply_untapped"
            default:
                reason = "live_not_narrating"
            }
        }
    }

    /// The narration's verdict, from all three layers. Layers 1 and 2 can say `silent` on
    /// their own; only layer 3 can say `audible`.
    private static func narrationVerdict(_ probe: PlaybackProbe) -> (verdict: Verdict, reason: String) {
        if probe.transport != .playing {
            return (.silent, PlaybackProbe.SilenceReason.notPlaying.rawValue)
        }
        if probe.advancedMs <= 0 {
            return (.silent, PlaybackProbe.SilenceReason.clockNotMoving.rawValue)
        }
        guard let level = probe.level else { return (.unmeasured, "no_tap") }
        return level.isSounding
            ? (.audible, "none")
            : (.silent, PlaybackProbe.SilenceReason.noAudioRendered.rawValue)
    }

    // MARK: - Rendering

    /// `m:ss`, the way a person reads a position off a video.
    public var positionText: String {
        let seconds = max(0, positionMs) / 1_000
        return String(format: "%d:%02d", locale: Locale(identifier: "en_US_POSIX"), seconds / 60, seconds % 60)
    }

    /// The RMS as text: four decimals, `.` always, or `unmeasured`.
    public var rmsText: String {
        rms.map(PlaybackProbe.number) ?? "unmeasured"
    }

    /// The bar's fill, 0…1, on a dBFS scale from ``AudioReadout/barFloorDbfs`` to 0.
    ///
    /// Logarithmic because a linear RMS bar is empty for speech: half-scale sine is 0.35
    /// RMS, and narration sits near 0.05, which would be a sliver nobody can see in a video
    /// frame. Nil where nothing measured, so the view draws "no bar" rather than an empty
    /// one that would read as silence.
    public var barFraction: Double? {
        guard let rms else { return nil }
        guard rms > 0 else { return 0 }
        let dbfs = 20 * log10(rms)
        return min(1, max(0, (dbfs - Self.barFloorDbfs) / -Self.barFloorDbfs))
    }

    /// The bottom of the bar. The probe's own silence floor is -66 dBFS, so anything the
    /// probe calls sounding draws as visibly more than nothing.
    public static let barFloorDbfs: Double = -72

    /// The same fields as one `key=value` line, the shape ``PlaybackProbe/summary`` uses, so
    /// a driver can parse one element instead of eight.
    public var summary: String {
        [
            "mode=\(mode.rawValue)",
            "verdict=\(verdict.rawValue)",
            "reason=\(reason)",
            "transport=\(transport.rawValue)",
            "position_ms=\(positionMs)",
            "clock=\(clock.rawValue)",
            "rms=\(rmsText)",
            "peak=\(peak.map(PlaybackProbe.number) ?? "unmeasured")",
            "route=\(route)",
            "live_phase=\(livePhase ?? "none")",
        ].joined(separator: " ")
    }

    /// The accessibility identifiers the readout's elements carry. Stable: an automated
    /// driver (strad#369's `read_screen`, `MotetUITests`) asserts on these exact strings, and
    /// `ios/README.md` documents them.
    public enum Identifier {
        public static let container = "audio-readout"
        public static let mode = "audio-readout-mode"
        public static let verdict = "audio-readout-verdict"
        public static let reason = "audio-readout-reason"
        public static let transport = "audio-readout-transport"
        public static let position = "audio-readout-position"
        public static let clock = "audio-readout-clock"
        public static let rms = "audio-readout-rms"
        public static let route = "audio-readout-route"
        public static let summary = "audio-readout-summary"
    }
}
