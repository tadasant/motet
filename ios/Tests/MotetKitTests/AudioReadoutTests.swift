import Foundation
import XCTest
@testable import MotetKit

/// motet#152: the readout a device-farm recording is read from. Every assertion here is a
/// claim a farm driver will make about a real phone, so the direction that matters is the
/// one where the readout says *more* than was measured — a false `audible`.
final class AudioReadoutTests: XCTestCase {
    private let speaker = AudioRoute(category: "AVAudioSessionCategoryPlayback", mode: "m", policy: "p", outputs: ["Speaker"])
    private let tone = AudioLevel(rms: 0.35, peak: 0.5, frames: 2_048)
    private let hush = AudioLevel(rms: 0, peak: 0, frames: 2_048)

    private func probe(
        _ transport: PlaybackTransport = .playing,
        advancedMs: Int = 1_000,
        level: AudioLevel? = nil,
        route: AudioRoute? = nil
    ) -> PlaybackProbe {
        PlaybackProbe(transport: transport, positionMs: 72_400, advancedMs: advancedMs, rate: 1, level: level, route: route)
    }

    private func live(_ phase: LiveSnapshot.Phase) -> LiveSnapshot {
        var snapshot = LiveSnapshot()
        snapshot.phase = phase
        return snapshot
    }

    // MARK: - Listen

    func testPlayingWithAMeasuredToneIsAudible() {
        let readout = AudioReadout(probe: probe(level: tone, route: speaker))
        XCTAssertEqual(readout.mode, .listen)
        XCTAssertEqual(readout.verdict, .audible)
        XCTAssertEqual(readout.reason, "none")
        XCTAssertEqual(readout.clock, .advancing)
        XCTAssertEqual(readout.route, "Speaker")
    }

    func testPausedIsSilentAndTheClockIsStopped() {
        let readout = AudioReadout(probe: probe(.paused, advancedMs: 0, level: tone))
        XCTAssertEqual(readout.verdict, .silent)
        XCTAssertEqual(readout.reason, "notPlaying")
        XCTAssertEqual(readout.clock, .stopped)
    }

    func testAFrozenClockIsSilentWhateverTheTapSays() {
        let readout = AudioReadout(probe: probe(advancedMs: 0, level: tone))
        XCTAssertEqual(readout.verdict, .silent)
        XCTAssertEqual(readout.reason, "clockNotMoving")
        XCTAssertEqual(readout.clock, .frozen)
    }

    func testAMeasuredHushIsSilent() {
        let readout = AudioReadout(probe: probe(level: hush))
        XCTAssertEqual(readout.verdict, .silent)
        XCTAssertEqual(readout.reason, "noAudioRendered")
    }

    /// The one place this is stricter than `PlaybackProbe.isAudible`: no tap is not
    /// evidence of sound. Nor is it evidence of silence.
    func testNoTapIsUnmeasuredNeverAudibleAndNeverSilent() {
        let source = probe(level: nil)
        XCTAssertTrue(source.isAudible, "the probe abstains and lets layers 1 and 2 decide")
        let readout = AudioReadout(probe: source)
        XCTAssertEqual(readout.verdict, .unmeasured)
        XCTAssertEqual(readout.reason, "no_tap")
        XCTAssertEqual(readout.rmsText, "unmeasured")
        XCTAssertNil(readout.barFraction, "no bar, rather than an empty one that reads as silence")
    }

    func testAnIdleLiveSnapshotIsListenMode() {
        XCTAssertEqual(AudioReadout(probe: probe(level: tone), live: LiveSnapshot()).mode, .listen)
        XCTAssertEqual(AudioReadout(probe: probe(level: tone), live: live(.error)).mode, .listen)
    }

    // MARK: - Live

    func testLiveNarrationThatTheTapHearsIsAudible() {
        let readout = AudioReadout(probe: probe(level: tone), live: live(.narrating))
        XCTAssertEqual(readout.mode, .live)
        XCTAssertEqual(readout.verdict, .audible)
        XCTAssertEqual(readout.livePhase, "narrating")
    }

    /// The reply plays through `AVLiveAudio`'s engine, which no tap is on. Narration paused
    /// for the question is the expected state, and calling it silent would be a vote by a
    /// layer that is not there.
    func testEveryNonAudibleLiveMomentIsUnmeasured() {
        for phase in [LiveSnapshot.Phase.connecting, .narrating, .paused, .listening, .replying, .resuming] {
            for source in [probe(.paused, advancedMs: 0, level: tone), probe(advancedMs: 0), probe(level: hush), probe(level: nil)] {
                let readout = AudioReadout(probe: source, live: live(phase))
                XCTAssertEqual(readout.mode, .live, "\(phase)")
                XCTAssertEqual(readout.verdict, .unmeasured, "\(phase) \(source.summary)")
                XCTAssertEqual(readout.reason, "live_reply_untapped")
            }
        }
    }

    /// The property the issue names: in Live mode, nothing but a measured narration may
    /// read `audible`.
    func testLiveIsNeverAudibleWithoutAMeasuredSound() {
        for phase in [LiveSnapshot.Phase.connecting, .narrating, .paused, .listening, .replying, .resuming] {
            for transport in PlaybackTransport.allCases {
                for level in [nil, hush, tone] {
                    for advanced in [0, 1_000] {
                        let source = probe(transport, advancedMs: advanced, level: level)
                        let readout = AudioReadout(probe: source, live: live(phase))
                        let measuredSound = transport == .playing && advanced > 0 && level == tone
                        XCTAssertEqual(readout.verdict == .audible, measuredSound, "\(phase) \(source.summary)")
                    }
                }
            }
        }
    }

    // MARK: - Rendering

    func testFieldsRenderAsShortTokens() {
        let readout = AudioReadout(probe: probe(level: tone, route: AudioRoute(category: "c", mode: "m", policy: "p", outputs: ["Speaker", "AirPlay"])))
        XCTAssertEqual(readout.positionText, "1:12")
        XCTAssertEqual(readout.rmsText, "0.3500")
        XCTAssertEqual(readout.route, "Speaker+AirPlay")
        XCTAssertEqual(AudioReadout(probe: probe(route: AudioRoute(category: "c", mode: "m", policy: "p", outputs: []))).route, "none")
        XCTAssertEqual(AudioReadout(probe: probe()).route, "unknown")
    }

    func testTheBarIsLogarithmicAndClamped() throws {
        let full = try XCTUnwrap(AudioReadout(probe: probe(level: AudioLevel(rms: 1, peak: 1, frames: 1))).barFraction)
        XCTAssertEqual(full, 1, accuracy: 1e-9)
        XCTAssertEqual(AudioReadout(probe: probe(level: hush)).barFraction, 0)
        let speech = try XCTUnwrap(AudioReadout(probe: probe(level: AudioLevel(rms: 0.05, peak: 0.3, frames: 1))).barFraction)
        XCTAssertGreaterThan(speech, 0.5, "narration has to be visible in a video frame, not a sliver")
        let floor = try XCTUnwrap(AudioReadout(probe: probe(level: AudioLevel(rms: 0.0001, peak: 0.0001, frames: 1))).barFraction)
        XCTAssertEqual(floor, 0, accuracy: 1e-9, "-80 dBFS is below the bar's floor")
    }

    func testTheSummaryIsParseableKeyValue() {
        let line = AudioReadout(probe: probe(level: tone, route: speaker), live: live(.listening)).summary
        let fields = Dictionary(uniqueKeysWithValues: line.split(separator: " ").map { pair -> (String, String) in
            let parts = pair.split(separator: "=", maxSplits: 1)
            return (String(parts[0]), String(parts[1]))
        })
        XCTAssertEqual(fields["mode"], "live")
        XCTAssertEqual(fields["verdict"], "audible")
        XCTAssertEqual(fields["transport"], "playing")
        XCTAssertEqual(fields["position_ms"], "72400")
        XCTAssertEqual(fields["clock"], "advancing")
        XCTAssertEqual(fields["rms"], "0.3500")
        XCTAssertEqual(fields["route"], "Speaker")
        XCTAssertEqual(fields["live_phase"], "listening")
    }

    func testIdentifiersAreDistinctAndPrefixed() {
        let ids = [
            AudioReadout.Identifier.container, AudioReadout.Identifier.mode, AudioReadout.Identifier.verdict,
            AudioReadout.Identifier.reason, AudioReadout.Identifier.transport, AudioReadout.Identifier.position,
            AudioReadout.Identifier.clock, AudioReadout.Identifier.rms, AudioReadout.Identifier.route,
            AudioReadout.Identifier.summary,
        ]
        XCTAssertEqual(Set(ids).count, ids.count)
        XCTAssertTrue(ids.allSatisfy { $0.hasPrefix("audio-readout") })
    }
}
