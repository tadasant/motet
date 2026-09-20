import Foundation

/// **"Is sound actually coming out" as a value a machine can assert on.**
///
/// No cloud device service captures iOS audio — AWS Device Farm's session artifacts are
/// video, logs and screenshots with no audio track, and Appetize deprecated iOS audio
/// output and never supported microphone input at all. So the question cannot be answered
/// by watching the app from outside; it has to be answered by the app about itself. This
/// is that answer, in three layers of decreasing cheapness and increasing proof:
///
/// 1. **What the player says it is doing** — `transport`, `rate`, `waitReason`, `error`.
///    Free, and enough to tell "told to play" from "playing".
/// 2. **Whether the clock is actually moving** — `advancedMs` over ``PlaybackClockWatch``'s
///    window. This is the layer that catches the failure that started all of this: a player
///    in `.waitingToPlayAtSpecifiedRate` reports `readyToPlay`, sets no error and never
///    ticks, so everything except the clock says it is fine.
/// 3. **The audio being rendered** — `level`, an RMS/peak measurement taken off the
///    player's own audio tap. This is the only layer that distinguishes "the clock is
///    running and the speaker is silent" from "it works", and it is the one that turns
///    "can I hear it" into a number.
///
/// Layer 3 is optional on purpose. It needs a real `MTAudioProcessingTap`, which exists
/// only where AVFoundation does and only once an audio track has loaded; where it is
/// absent `level` is nil and ``isAudible`` falls back to layers 1 and 2 rather than
/// answering `false` about something it did not measure.
public struct PlaybackProbe: Hashable, Sendable {
    /// What `AVPlayer.timeControlStatus` reports, named so `MotetKit` can hold it.
    public var transport: PlaybackTransport
    /// The engine's own clock, in milliseconds from the start of the episode.
    public var positionMs: Int
    /// How far the clock moved over the watch window. Zero while paused, and — the
    /// interesting case — zero while `transport` is `.playing` and nothing is coming out.
    public var advancedMs: Int
    /// The rate the player is running at, which is not the rate it was *asked* for.
    public var rate: Double
    /// `AVPlayer.reasonForWaitingToPlay`, where there is one.
    public var waitReason: PlaybackWaitReason?
    /// What the audio tap measured, or nil where nothing is measuring.
    public var level: AudioLevel?
    /// The audio session as iOS actually has it — which is not what was asked for when a
    /// `setCategory` was refused.
    public var route: AudioRoute?
    public var errorMessage: String?
    /// Where the probe is looking. Not part of the verdict; it is what makes a log line
    /// from a real device attributable to an episode.
    public var episodeId: String?

    public init(
        transport: PlaybackTransport = .paused,
        positionMs: Int = 0,
        advancedMs: Int = 0,
        rate: Double = 0,
        waitReason: PlaybackWaitReason? = nil,
        level: AudioLevel? = nil,
        route: AudioRoute? = nil,
        errorMessage: String? = nil,
        episodeId: String? = nil
    ) {
        self.transport = transport
        self.positionMs = positionMs
        self.advancedMs = advancedMs
        self.rate = rate
        self.waitReason = waitReason
        self.level = level
        self.route = route
        self.errorMessage = errorMessage
        self.episodeId = episodeId
    }

    /// **The one-line verdict, and the thing a UI test asserts on.**
    ///
    /// Every layer present has to agree. A missing layer abstains rather than voting no:
    /// `level == nil` means nothing measured the audio, which is not the same claim as
    /// "the audio was silent", and conflating them would make a build without the tap
    /// report a fault it has no evidence for.
    public var isAudible: Bool {
        guard transport == .playing, advancedMs > 0 else { return false }
        guard let level else { return true }
        return level.isSounding
    }

    /// Why ``isAudible`` is false, in the listener's terms — nil when it is true.
    ///
    /// The three answers are genuinely different repairs, which is the whole reason this
    /// is not a bool: nobody pressed play, the player was told to play and is not, or the
    /// player is running and the speaker is silent.
    public var silenceReason: SilenceReason? {
        if isAudible { return nil }
        if transport != .playing { return .notPlaying }
        if advancedMs <= 0 { return .clockNotMoving }
        return .noAudioRendered
    }

    public enum SilenceReason: String, Hashable, Sendable {
        /// Nobody asked for audio, or the player is paused or waiting.
        case notPlaying
        /// The player says it is playing and its clock is frozen.
        case clockNotMoving
        /// The clock is running and the tap measured nothing above the noise floor.
        case noAudioRendered
    }

    /// The structured line that goes to the log and to the debug overlay's accessibility
    /// value — one string, so the assertion a UI test makes and the line an operator reads
    /// off a real device's Console are the same statement.
    ///
    /// `key=value`, space-separated, no commas or quotes, because it is parsed by a test:
    /// `audible=false transport=playing advanced_ms=0 …` is what a frozen player looks
    /// like, and it is greppable.
    public var summary: String {
        var parts: [String] = [
            "audible=\(isAudible)",
            "transport=\(transport.rawValue)",
            "position_ms=\(positionMs)",
            "advanced_ms=\(advancedMs)",
            "rate=\(Self.number(rate))",
        ]
        parts.append("silence=\(silenceReason?.rawValue ?? "none")")
        parts.append("wait=\(waitReason?.rawValue ?? "none")")
        if let level {
            parts.append("rms=\(Self.number(level.rms))")
            parts.append("peak=\(Self.number(level.peak))")
            parts.append("sounding=\(level.isSounding)")
        } else {
            parts.append("rms=unmeasured")
        }
        if let route {
            parts.append("category=\(route.category)")
            parts.append("mode=\(route.mode)")
            parts.append("policy=\(route.policy)")
            parts.append("output=\(route.outputs.isEmpty ? "none" : route.outputs.joined(separator: "+"))")
        }
        parts.append("error=\(errorMessage == nil ? "none" : "yes")")
        return parts.joined(separator: " ")
    }

    /// Four decimal places, `.` always, and never scientific notation: a test parses this
    /// and a locale that writes `0,0012` would make it unparseable on a phone in Vilnius.
    static func number(_ value: Double) -> String {
        String(format: "%.4f", locale: Locale(identifier: "en_US_POSIX"), value)
    }
}

/// `AVPlayer.timeControlStatus`'s three states, spelled where `MotetKit` can reason about
/// them. **All three**: the engine used to observe two, and the missing one is exactly the
/// state a real device sits in when a remote asset will not start.
public enum PlaybackTransport: String, Hashable, Sendable, CaseIterable {
    case paused
    case waiting
    case playing
}

/// What the audio tap measured over its most recent window.
public struct AudioLevel: Hashable, Sendable {
    /// Root mean square over the window, 0…1 in sample units.
    public var rms: Double
    /// The largest absolute sample in the window, 0…1.
    public var peak: Double
    /// How many frames the current window covers.
    ///
    /// Zero here **is** evidence of silence, and the abstention lives one layer out:
    /// `AudioLevelMeter.level()` answers `nil` until a buffer has ever arrived and for a
    /// format it cannot read, so an `AudioLevel` existing at all means something measured.
    /// Given that, a window with no frames is a render that produced nothing.
    public var frames: Int

    public init(rms: Double, peak: Double, frames: Int) {
        self.rms = rms
        self.peak = peak
        self.frames = frames
    }

    /// Above the floor below which a rendered stream is indistinguishable from digital
    /// silence.
    ///
    /// -66 dBFS. Chosen below anything a listener would call audible and well above the
    /// dither a decoder leaves in a silent passage, so a narration pause does not read as
    /// a fault — the window is ``PlaybackClockWatch/window`` seconds of *peak*, and speech
    /// does not have two seconds of true silence in it at that depth.
    public static let silenceFloor: Double = 0.0005

    /// Whether this measurement is of audio rather than of silence. Peak rather than RMS,
    /// because RMS over a window containing one loud word and a lot of pause is small.
    public var isSounding: Bool { frames > 0 && peak > Self.silenceFloor }
}

/// The audio session as iOS has it, rather than as it was asked for.
///
/// It belongs on the probe because it is the *other* reason a phone is silent, and the one
/// a listener cannot see: a refused `setCategory` leaves the session on `.soloAmbient`,
/// which is muted by the ringer switch and stops on lock, while every other signal in the
/// app says playback is fine.
public struct AudioRoute: Hashable, Sendable {
    public var category: String
    public var mode: String
    public var policy: String
    /// The current route's output port types — `Speaker`, `BluetoothA2DPOutput`, … Empty
    /// is a real answer and a bad one: a session with no output renders to nothing.
    public var outputs: [String]
    public var isOtherAudioPlaying: Bool

    public init(
        category: String,
        mode: String,
        policy: String,
        outputs: [String],
        isOtherAudioPlaying: Bool = false
    ) {
        self.category = category
        self.mode = mode
        self.policy = policy
        self.outputs = outputs
        self.isOtherAudioPlaying = isOtherAudioPlaying
    }
}
