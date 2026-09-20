import Foundation

/// Which shape of audio session to ask iOS for, in the order to ask.
///
/// **A refused `setCategory` used to be swallowed**, with `try?`, at both call sites. That
/// is the quietest failure this app has: the session stays on the process default,
/// `.soloAmbient`, which is *silenced by the ringer switch* and *stops when the screen
/// locks* — so a phone on silent plays a briefing nobody can hear, and a phone in a pocket
/// stops, and nothing anywhere says why. For a podcast player that is the whole product
/// failing in the one configuration most phones are actually in.
///
/// iOS validates the category, the mode, the route-sharing policy and the options
/// *together*, and which combinations it accepts has changed between releases — 18.5
/// tightened `longFormAudio` so that it refuses any explicit `CategoryOptions`. That
/// particular tightening is not what this app hit, because it already passed none; the
/// point it makes is the general one, that a single hard-coded combination is a bet on a
/// validation table that moves under it with no warning and no way to find out.
///
/// So this is a ladder rather than a constant: ask for the best shape, and on a refusal
/// step down to one that gives up a nicety rather than the product. Every rung keeps
/// `.playback` and passes no options, because `.playback` *is* the mute-switch and
/// background-audio behaviour; what the lower rungs give up is the long-form route sharing
/// (AirPlay 2 grouping with other podcast apps) and then the spoken-audio ducking. Running
/// out of rungs is an error the listener is told about, not a `try?`.
///
/// This type is `MotetKit` rather than `MotetPlayback` deliberately. The ladder, the rule
/// for what a rung costs, and the walk down it are the parts that can be *wrong*, and they
/// are testable on a machine with no AVFoundation at all — which every machine in this
/// project's CI is except one hosted macOS runner. `AudioSessionController` supplies one
/// closure that calls `AVAudioSession` and does nothing else.
public struct AudioSessionShape: Hashable, Sendable {
    /// `AVAudioSession.Mode`, named so `MotetKit` can hold it.
    public enum Mode: String, Hashable, Sendable {
        /// `.spokenAudio` — what Apple defines for this content. Navigation prompts duck
        /// the briefing instead of talking over it.
        case spokenAudio
        case standard
    }

    /// `AVAudioSession.RouteSharingPolicy`, likewise.
    public enum Policy: String, Hashable, Sendable {
        /// `.longFormAudio` — shares an AirPlay 2 route with other long-form audio apps.
        case longFormAudio
        case standard
    }

    public let mode: Mode
    public let policy: Policy
    /// What is given up relative to the rung above, for the log. Empty on the first.
    public let concession: String

    public init(mode: Mode, policy: Policy, concession: String = "") {
        self.mode = mode
        self.policy = policy
        self.concession = concession
    }

    public var label: String { "playback/\(mode.rawValue)/\(policy.rawValue)" }
}

public enum AudioSessionPlan {
    /// What walking the ladder achieved.
    public enum Outcome: Equatable, Sendable {
        /// A rung took. `shape` says which; anything but the first gave something up.
        case configured(AudioSessionShape)
        /// Every rung was refused. `errors` is one line per rung.
        case refused(errors: [String])

        public var isRefusal: Bool {
            if case .refused = self { return true }
            return false
        }

        /// What the **listener** is told, or nil when there is nothing they need to know.
        ///
        /// A lower rung that took is a player that works: the listener does not care that
        /// AirPlay 2 grouping is off, and putting that on the player screen would train
        /// them to ignore the one line that means the audio will be silent. So a concession
        /// goes to the log and nowhere else, and this is nil for every `configured` case.
        public var listenerMessage: String? {
            switch self {
            case .configured: return nil
            case .refused: return AudioSessionPlan.noShapeAccepted
            }
        }

        /// One line for the log, always — including the ordinary case, because "which shape
        /// did this phone accept" is the first question to ask of a silent device and the
        /// only place it can be answered from is a log the listener never sees.
        public var logLine: String {
            switch self {
            case .configured(let shape) where shape.concession.isEmpty:
                return "audio session: \(shape.label)"
            case .configured(let shape):
                return "audio session fell back to \(shape.label) — \(shape.concession)"
            case .refused(let errors):
                return "audio session: every shape refused — \(errors.joined(separator: "; "))"
            }
        }
    }

    /// The listening ladder, best first.
    public static let listening: [AudioSessionShape] = [
        AudioSessionShape(mode: .spokenAudio, policy: .longFormAudio),
        AudioSessionShape(
            mode: .spokenAudio,
            policy: .standard,
            concession: "no long-form route sharing (AirPlay 2 grouping)"
        ),
        AudioSessionShape(
            mode: .standard,
            policy: .standard,
            concession: "no spoken-audio ducking, and no long-form route sharing"
        ),
    ]

    /// What the listener is told when every rung was refused.
    ///
    /// Named here rather than written at the call site because it is the sentence that
    /// replaces the silence this whole type exists to remove, and a test asserts it.
    public static let noShapeAccepted =
        "This phone would not let Motet take the audio output. Playback will be silent "
        + "while the ringer switch is on, and will stop when the screen locks."

    /// Walk the ladder with `apply`, stopping at the first rung it does not throw on.
    ///
    /// The whole of the decision, and none of AVFoundation — so the thing that can actually
    /// be wrong (does a throw on the first rung fall through to the second, is every
    /// refusal kept, is the right rung reported) is testable where `AVAudioSession` does
    /// not exist.
    public static func walk(_ apply: (AudioSessionShape) throws -> Void) -> Outcome {
        var errors: [String] = []
        for shape in listening {
            do {
                try apply(shape)
                return .configured(shape)
            } catch {
                errors.append("\(shape.label): \(error.localizedDescription)")
            }
        }
        return .refused(errors: errors)
    }
}
