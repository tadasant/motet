import Foundation

/// Which shape of audio session to ask iOS for, in the order to ask.
///
/// **A refused `setCategory` used to be swallowed**, in three places, with `try?`. That is
/// the quietest failure this app has: the session stays on the process default,
/// `.soloAmbient`, which is *silenced by the ringer switch* and *stops when the screen
/// locks* — so a phone on silent plays a briefing nobody can hear, and a phone in a pocket
/// stops, and nothing anywhere says why. For a podcast player that is the whole product
/// failing in the one configuration most phones are actually in.
///
/// iOS validates the category, the mode, the route-sharing policy and the options
/// *together*, and which combinations it accepts has changed between releases — 18.5
/// tightened `longFormAudio` so that it refuses any explicit `CategoryOptions` at all. A
/// single hard-coded combination is therefore a bet on a validation table that moves.
///
/// So this is a ladder rather than a constant: ask for the best shape, and on a refusal
/// step down to one that gives up a nicety rather than the product. Every rung keeps
/// `.playback`, because `.playback` *is* the mute-switch and background-audio behaviour;
/// what the lower rungs give up is the long-form route sharing (AirPlay 2 grouping with
/// other podcast apps) and then the spoken-audio ducking. Running out of rungs is an error
/// the listener is told about, not a `try?`.
///
/// This type is `MotetKit` rather than `MotetPlayback` deliberately: the ladder and the
/// rule for what a rung costs are the part that can be *wrong*, and they are testable on
/// a machine with no AVFoundation at all. `AudioSessionController` does nothing but walk
/// it and call `AVAudioSession`.
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
    /// What is given up relative to the rung above, for the log line. Empty on the first.
    public let concession: String

    public init(mode: Mode, policy: Policy, concession: String = "") {
        self.mode = mode
        self.policy = policy
        self.concession = concession
    }

    public var label: String { "playback/\(mode.rawValue)/\(policy.rawValue)" }
}

public enum AudioSessionPlan {
    /// The listening ladder, best first. Every rung is the `.playback` category with no
    /// explicit options — iOS 18.5 refuses options alongside `longFormAudio`, and none of
    /// the options this app would want are worth a rung of their own.
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
    /// replaces the silence this whole type exists to remove, and a test asserts the UI
    /// shows it.
    public static let noShapeAccepted =
        "This phone would not let Motet take the audio output. Playback will be silent "
        + "while the ringer switch is on, and will stop when the screen locks."

    /// What to say about a rung that took but was not the first one.
    ///
    /// `nil` for the ideal rung: nothing is owed to a listener when nothing was given up.
    public static func concessionNote(for shape: AudioSessionShape) -> String? {
        shape.concession.isEmpty ? nil : "Audio is set up with \(shape.concession)."
    }
}
