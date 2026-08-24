import Foundation
import MotetKit

#if canImport(MediaPlayer) && os(iOS)
import MediaPlayer

/// The lockscreen, the Control Centre, the CarPlay dashboard, and the steering-wheel remote.
///
/// All four render `MPNowPlayingInfoCenter` and send `MPRemoteCommandCenter` commands, and
/// every one of those commands maps to a `PlaybackCommand` — the deterministic set. A remote
/// press does exactly what the on-screen button does, including with no signal, because
/// nothing in this path consults a model or the network.
///
/// The elapsed time published here is **ours**, taken from the controller's snapshot rather
/// than from the player (invariant 4). `MPNowPlayingInfoPropertyElapsedPlaybackTime` is a
/// fixed point that iOS extrapolates from using the playback rate, so publishing a rate of
/// 1.0 while playing at 1.5 makes the lockscreen scrubber drift visibly.
///
/// **Unverified here.** There is no lockscreen on a Linux CI runner, and the simulator's
/// Control Centre is not the device's. See `ios/README.md`.
public final class NowPlayingController: @unchecked Sendable {
    private let commandCenter = MPRemoteCommandCenter.shared()
    private let infoCenter = MPNowPlayingInfoCenter.default()

    public init() {}

    /// Wire the remote command centre to the controller.
    public func attach(
        to controller: PlaybackController,
        settings: PlaybackSettings
    ) {
        commandCenter.playCommand.addTarget { _ in
            Task { await controller.perform(.play) }
            return .success
        }
        commandCenter.pauseCommand.addTarget { _ in
            Task { await controller.perform(.pause) }
            return .success
        }
        commandCenter.togglePlayPauseCommand.addTarget { _ in
            Task { await controller.perform(.togglePlayPause) }
            return .success
        }

        commandCenter.skipForwardCommand.preferredIntervals = [
            NSNumber(value: Double(settings.skipForwardMs) / 1_000)
        ]
        commandCenter.skipForwardCommand.addTarget { _ in
            Task { await controller.perform(.skipForward) }
            return .success
        }
        commandCenter.skipBackwardCommand.preferredIntervals = [
            NSNumber(value: Double(settings.skipBackwardMs) / 1_000)
        ]
        commandCenter.skipBackwardCommand.addTarget { _ in
            Task { await controller.perform(.skipBackward) }
            return .success
        }

        // Next/previous *track* on a remote means next/previous story here. An episode is
        // one audio file, so without this a steering-wheel press would do nothing.
        commandCenter.nextTrackCommand.addTarget { _ in
            Task { await controller.perform(.nextSegment) }
            return .success
        }
        commandCenter.previousTrackCommand.addTarget { _ in
            Task { await controller.perform(.previousSegment) }
            return .success
        }

        commandCenter.changePlaybackPositionCommand.addTarget { event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            Task { await controller.perform(.seek(toMs: Int(event.positionTime * 1_000))) }
            return .success
        }

        commandCenter.changePlaybackRateCommand.supportedPlaybackRates =
            PlaybackSettings.rateLadder.map { NSNumber(value: $0) }
        commandCenter.changePlaybackRateCommand.addTarget { event in
            guard let event = event as? MPChangePlaybackRateCommandEvent else { return .commandFailed }
            Task { await controller.perform(.setRate(Double(event.playbackRate))) }
            return .success
        }
    }

    /// Publish what is playing. Called on every snapshot the controller emits.
    public func update(with snapshot: PlaybackSnapshot) {
        guard snapshot.hasEpisode else {
            infoCenter.nowPlayingInfo = nil
            return
        }
        var info: [String: Any] = [:]
        info[MPMediaItemPropertyTitle] = snapshot.currentSegmentTitle ?? snapshot.episodeTitle
        info[MPMediaItemPropertyAlbumTitle] = snapshot.episodeTitle
        info[MPMediaItemPropertyArtist] = "Motet"
        info[MPMediaItemPropertyPlaybackDuration] = Double(snapshot.durationMs) / 1_000
        info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = Double(snapshot.positionMs) / 1_000
        info[MPNowPlayingInfoPropertyPlaybackRate] = snapshot.isPlaying ? snapshot.rate : 0.0
        info[MPNowPlayingInfoPropertyDefaultPlaybackRate] = snapshot.rate
        info[MPNowPlayingInfoPropertyIsLiveStream] = false
        // A spoken briefing, not music: this is what makes CarPlay and the lockscreen show
        // podcast-shaped controls.
        info[MPNowPlayingInfoPropertyMediaType] = MPNowPlayingInfoMediaType.audio.rawValue
        infoCenter.nowPlayingInfo = info
        infoCenter.playbackState = snapshot.isPlaying ? .playing : .paused
    }
}
#endif
