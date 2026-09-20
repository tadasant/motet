import Foundation
import MotetKit

#if canImport(AVFoundation) && os(iOS)
import AVFoundation
import os

/// The audio session: what makes Motet audible at all, and what keeps it playing with the
/// screen locked.
///
/// Three things have to line up, and dropping any one of them turns a dog walk into
/// silence the moment the phone goes in a pocket:
///
/// 1. the `audio` background mode in `Info.plist` (see `App/Motet/Info.plist`);
/// 2. the `.playback` category, which is also what keeps playing when the ringer is
///    silenced — an app whose audio stops on the mute switch is not a podcast player;
/// 3. an *active* session, activated when playback starts rather than at launch, so Motet
///    does not interrupt whatever the phone was already playing just by being opened.
///
/// **The category is asked for as a ladder, and a refusal is reported rather than
/// swallowed.** This used to be one hard-coded combination behind `try?`, at both call
/// sites. iOS validates category, mode, route-sharing policy and options *together*, and
/// that validation has tightened between releases; when it refuses, the session stays on
/// the process default `.soloAmbient`, which is muted by the ringer switch and stops on
/// lock. So the failure mode of a swallowed `setCategory` is *exactly* the bug report
/// "I can't hear anything" — with nothing in the app or the logs saying so.
///
/// ``AudioSessionPlan`` holds the rungs, what each gives up, and the walk down them; this
/// class supplies the one closure that calls `AVAudioSession` and logs what came back. The
/// split is so that the half which can be wrong is testable where AVFoundation does not
/// exist, which is every machine in CI but one.
///
/// **Unverified here.** The simulator has an audio session API that accepts all of this and
/// a host OS that does not enforce it: background audio, the mute switch, and ducking are
/// device behaviours. See `ios/README.md`.
public final class AudioSessionController: @unchecked Sendable {
    private static let logger = Logger(subsystem: "com.getmotet.app", category: "audio-session")

    public init() {}

    /// Put the session into the best listening shape this phone will accept.
    ///
    /// Never throws: running out of rungs is an answer — ``AudioSessionPlan/Outcome/refused``
    /// — not an exception, because every caller's only sane response is to say so and carry
    /// on trying to play. A caller that threw here would have to choose between refusing to
    /// play at all and swallowing it again.
    @discardableResult
    public func configure() -> AudioSessionPlan.Outcome {
        let session = AVAudioSession.sharedInstance()
        let outcome = AudioSessionPlan.walk { shape in
            try session.setCategory(
                .playback,
                mode: Self.mode(shape.mode),
                policy: Self.policy(shape.policy),
                options: []
            )
        }
        // Logged on every path, the ordinary one included: "which shape did this phone
        // accept" is the first question to ask of a silent device, and a log line is the
        // artefact that travels off somebody else's phone when the screen does not.
        let line = outcome.logLine
        switch outcome {
        case .configured(let shape) where shape.concession.isEmpty:
            Self.logger.notice("\(line, privacy: .public)")
        case .configured:
            Self.logger.warning("\(line, privacy: .public)")
        case .refused:
            Self.logger.error(
                "\(line, privacy: .public). Playback will be silent under the ringer switch and will stop on lock."
            )
        }
        return outcome
    }

    public func activate() throws {
        try AVAudioSession.sharedInstance().setActive(true)
    }

    /// Hand the session back when nothing is playing, so other apps resume.
    public func deactivate() throws {
        try AVAudioSession.sharedInstance().setActive(
            false, options: [.notifyOthersOnDeactivation]
        )
    }

    /// Headphones pulled out, or a Bluetooth device disconnecting.
    ///
    /// `.oldDeviceUnavailable` is the one that matters: iOS keeps playing out of the phone
    /// speaker, which on a walk means a briefing suddenly broadcast to the street.
    public func observeRouteChanges(
        onOldDeviceUnavailable: @escaping @Sendable () -> Void
    ) -> NSObjectProtocol {
        NotificationCenter.default.addObserver(
            forName: AVAudioSession.routeChangeNotification,
            object: AVAudioSession.sharedInstance(),
            queue: .main
        ) { note in
            guard let raw = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
                  let reason = AVAudioSession.RouteChangeReason(rawValue: raw),
                  reason == .oldDeviceUnavailable else { return }
            onOldDeviceUnavailable()
        }
    }

    private static func mode(_ mode: AudioSessionShape.Mode) -> AVAudioSession.Mode {
        switch mode {
        case .spokenAudio: return .spokenAudio
        case .standard: return .default
        }
    }

    private static func policy(_ policy: AudioSessionShape.Policy) -> AVAudioSession.RouteSharingPolicy {
        switch policy {
        case .longFormAudio: return .longFormAudio
        case .standard: return .default
        }
    }
}
#endif
