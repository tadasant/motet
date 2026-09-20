import Foundation

/// Why the player was told to play and is producing no audio.
///
/// **This is the state that had no name, and that is the whole reason it exists.** The
/// engine reported `playing`, `paused`, `stalled`, `ended` and `failed` — and `AVPlayer`'s
/// most common real-device outcome is none of them. `timeControlStatus` goes to
/// `.waitingToPlayAtSpecifiedRate`, `reasonForWaitingToPlay` says why, the clock does not
/// move, no error is ever emitted, and the app happily draws a pause button over silence.
/// A play button that does nothing and says nothing is the bug; a missing observation is
/// how it got there.
///
/// The cases are `AVPlayer.WaitingReason`'s, spelled here so that `MotetKit` — and its
/// tests, which run where AVFoundation does not exist — can reason about them.
public enum PlaybackWaitReason: String, Hashable, Sendable, CaseIterable {
    case noItemToPlay
    case toMinimizeStalls
    case evaluatingBufferingRate
    case interstitialEvent
    case waitingForCoordinatedPlayback
    /// A reason this version of iOS has and this app does not know about. Reported rather
    /// than swallowed: "waiting for a reason nobody here recognises" is still an answer,
    /// and an unrecognised one is exactly the kind that never gets looked at otherwise.
    case unknown

    /// What the listener is told while it is still plausibly temporary.
    public var sentence: String {
        switch self {
        case .noItemToPlay:
            return "The player has nothing loaded to play."
        case .toMinimizeStalls:
            return "Buffering — waiting for enough audio to play without stopping."
        case .evaluatingBufferingRate:
            return "Measuring the connection before starting."
        case .interstitialEvent:
            return "Waiting on an interstitial."
        case .waitingForCoordinatedPlayback:
            return "Waiting for the other devices in this shared session."
        case .unknown:
            return "The player is waiting to start, and did not say why."
        }
    }

    /// Whether waiting for this reason can be expected to clear on its own.
    ///
    /// `noItemToPlay` cannot: there is nothing arriving that would end the wait, so it is
    /// an error the moment it is seen rather than after a grace period. Everything else is
    /// a network or a buffering wait, which usually does clear — and which
    /// ``PlaybackController/stallGraceSeconds`` bounds when it does not.
    public var clearsItself: Bool { self != .noItemToPlay }

    /// What the listener is told once the wait has outlasted its grace, or immediately
    /// when it was never going to clear.
    public var failureSentence: String {
        switch self {
        case .noItemToPlay:
            return "The player has nothing loaded to play. Try again."
        default:
            return "The audio would not start playing. \(sentence)"
        }
    }
}
