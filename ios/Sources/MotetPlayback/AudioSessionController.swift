import Foundation
import MotetKit

#if canImport(AVFoundation) && os(iOS)
import AVFoundation

/// The audio session: what makes Motet keep playing with the screen locked.
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
/// `.spokenAudio` is the mode Apple defines for exactly this content, and it is what makes
/// "duck other audio" and CarPlay behave: navigation prompts duck the briefing instead of
/// talking over it.
///
/// **Unverified here.** The simulator has an audio session API that accepts all of this and
/// a host OS that does not enforce it: background audio, the mute switch, and ducking are
/// device behaviours. See `ios/README.md`.
public final class AudioSessionController: @unchecked Sendable {
    public init() {}

    public func configure() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playback,
            mode: .spokenAudio,
            policy: .longFormAudio,
            options: []
        )
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
}
#endif
