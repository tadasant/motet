import XCTest
@testable import MotetKit

/// The rules behind "is sound coming out", tested where AVFoundation does not exist.
///
/// This is the same split `AudioSessionPlan` makes: the part that can be *wrong* — when a
/// frozen clock counts as frozen, when an unmeasured level abstains, what the one-line
/// verdict says — is data and arithmetic in `MotetKit`, and the AVFoundation file does
/// nothing but read `timeControlStatus` and hand over samples.
final class PlaybackClockWatchTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_800_000_000)

    func test_one_sample_is_not_a_measurement() {
        var watch = PlaybackClockWatch()
        watch.observe(positionMs: 5_000, at: start)
        // The alternative — "it has not moved" — would call every freshly started player
        // frozen, which is the false alarm this type exists to avoid.
        XCTAssertEqual(watch.advancedMs, 0)
        XCTAssertFalse(watch.isConclusive)
    }

    func test_a_running_clock_reports_what_it_covered() {
        var watch = PlaybackClockWatch()
        for tick in 0...3 {
            watch.observe(
                positionMs: 5_000 + tick * 1_000,
                at: start.addingTimeInterval(Double(tick))
            )
        }
        XCTAssertEqual(watch.advancedMs, 3_000)
        XCTAssertTrue(watch.isConclusive)
    }

    func test_a_frozen_clock_reports_zero_over_a_conclusive_window() {
        var watch = PlaybackClockWatch()
        for tick in 0...6 {
            watch.observe(positionMs: 5_000, at: start.addingTimeInterval(Double(tick) * 0.5))
        }
        XCTAssertEqual(watch.advancedMs, 0)
        XCTAssertTrue(watch.isConclusive, "three seconds of samples has to be enough to judge")
    }

    func test_sampling_faster_than_the_player_reports_does_not_shrink_the_window() {
        // `AVPlayer` reports about once a second; the app polls faster than that. A window
        // that kept only the samples strictly inside it would collapse to two identical
        // positions 80ms apart, and a working player would read as frozen.
        var watch = PlaybackClockWatch()
        var elapsed = 0.0
        while elapsed <= 3.0 {
            // The position only moves once a second, as a real player's does.
            watch.observe(positionMs: 5_000 + Int(elapsed) * 1_000, at: start.addingTimeInterval(elapsed))
            elapsed += 0.08
        }
        XCTAssertGreaterThan(watch.advancedMs, 0)
        XCTAssertGreaterThanOrEqual(watch.spanSeconds, PlaybackClockWatch.window)
    }

    func test_a_backwards_jump_is_never_progress() {
        var watch = PlaybackClockWatch()
        watch.observe(positionMs: 60_000, at: start)
        watch.observe(positionMs: 10_000, at: start.addingTimeInterval(1))
        XCTAssertEqual(watch.advancedMs, 0, "a re-buffer or a resume is not listening")
    }

    func test_reset_forgets_the_window() {
        var watch = PlaybackClockWatch()
        watch.observe(positionMs: 0, at: start)
        watch.observe(positionMs: 2_000, at: start.addingTimeInterval(1))
        watch.reset()
        watch.observe(positionMs: 2_000, at: start.addingTimeInterval(2))
        XCTAssertEqual(watch.advancedMs, 0)
    }
}

final class PlaybackProbeVerdictTests: XCTestCase {
    func test_a_player_that_is_playing_and_advancing_is_audible_without_a_level() {
        // An unmeasured level abstains. Reporting `false` would be a claim about audio
        // nothing looked at, and it is what a build with no working tap would report on
        // every perfectly good episode.
        let probe = PlaybackProbe(transport: .playing, advancedMs: 1_000, level: nil)
        XCTAssertTrue(probe.isAudible)
        XCTAssertNil(probe.silenceReason)
        XCTAssertTrue(probe.summary.contains("rms=unmeasured"))
    }

    func test_the_failure_this_exists_for_is_playing_with_a_frozen_clock() {
        let probe = PlaybackProbe(transport: .playing, positionMs: 9_408, advancedMs: 0)
        XCTAssertFalse(probe.isAudible)
        XCTAssertEqual(probe.silenceReason, .clockNotMoving)
    }

    func test_a_running_clock_with_a_silent_mix_is_its_own_answer() {
        let probe = PlaybackProbe(
            transport: .playing,
            advancedMs: 2_000,
            level: AudioLevel(rms: 0, peak: 0, frames: 44_100)
        )
        XCTAssertFalse(probe.isAudible)
        XCTAssertEqual(probe.silenceReason, .noAudioRendered)
    }

    func test_a_player_told_to_play_that_never_started_is_not_reported_as_paused() {
        // `AVPlayer` sits in `.paused` when a play it was asked for never got going, and
        // `PlaybackWaitReason.notStarted` is the case the engine invents for it. Reading
        // `timeControlStatus` alone would answer "paused" about a player somebody pressed
        // Play on — the exact reassuring answer this whole mechanism exists to stop.
        let probe = PlaybackProbe(transport: .waiting, advancedMs: 0, waitReason: .notStarted)
        XCTAssertFalse(probe.isAudible)
        XCTAssertTrue(probe.summary.contains("transport=waiting"))
        XCTAssertTrue(probe.summary.contains("wait=notStarted"))
    }

    func test_a_paused_player_is_not_a_fault() {
        let probe = PlaybackProbe(transport: .paused, advancedMs: 0)
        XCTAssertEqual(probe.silenceReason, .notPlaying)
    }

    func test_waiting_is_not_playing() {
        let probe = PlaybackProbe(
            transport: .waiting, advancedMs: 0, waitReason: .toMinimizeStalls
        )
        XCTAssertFalse(probe.isAudible)
        XCTAssertEqual(probe.silenceReason, .notPlaying)
        XCTAssertTrue(probe.summary.contains("wait=toMinimizeStalls"))
    }

    func test_a_window_with_no_frames_is_not_sounding() {
        // The abstention lives one layer out — `AudioLevelMeter.level()` answers nil until
        // a buffer has ever arrived and for a format it cannot read. Given an `AudioLevel`
        // exists at all, something measured, so an empty window is a render that produced
        // nothing rather than a question nobody asked.
        let level = AudioLevel(rms: 0, peak: 0, frames: 0)
        XCTAssertFalse(level.isSounding)
    }

    func test_the_summary_is_parseable_and_locale_proof() {
        let probe = PlaybackProbe(
            transport: .playing,
            positionMs: 12_000,
            advancedMs: 1_000,
            rate: 1.5,
            level: AudioLevel(rms: 0.12, peak: 0.44, frames: 48_000),
            route: AudioRoute(
                category: "Playback", mode: "SpokenAudio", policy: "longFormAudio",
                outputs: ["Speaker"]
            )
        )
        let fields = Dictionary(
            uniqueKeysWithValues: probe.summary.split(separator: " ").map { field -> (String, String) in
                let halves = field.split(separator: "=", maxSplits: 1)
                return (String(halves[0]), String(halves[1]))
            }
        )
        XCTAssertEqual(fields["audible"], "true")
        XCTAssertEqual(fields["transport"], "playing")
        XCTAssertEqual(fields["advanced_ms"], "1000")
        XCTAssertEqual(fields["rate"], "1.5000")
        XCTAssertEqual(fields["sounding"], "true")
        XCTAssertEqual(fields["category"], "Playback")
        XCTAssertEqual(fields["output"], "Speaker")
        XCTAssertEqual(fields["silence"], "none")
    }

    func test_a_session_with_no_output_is_reported_rather_than_hidden() {
        let probe = PlaybackProbe(
            transport: .playing,
            advancedMs: 500,
            route: AudioRoute(category: "SoloAmbient", mode: "Default", policy: "default", outputs: [])
        )
        XCTAssertTrue(probe.summary.contains("output=none"))
        XCTAssertTrue(probe.summary.contains("category=SoloAmbient"))
    }
}

/// A `PlaybackEngineProbe` the tests drive, standing in for `AVPlayerPlaybackEngine`.
private actor FakeEngineProbe: PlaybackEngineProbe {
    private var facts = EngineFacts()
    func set(_ facts: EngineFacts) { self.facts = facts }
    func engineFacts() async -> EngineFacts { facts }
}

private struct FixedRoute: AudioRouteProbe {
    let route: AudioRoute?
    func routeFacts() -> AudioRoute? { route }
}

final class PlaybackProbeRecorderTests: XCTestCase {
    func test_it_answers_the_question_the_ui_test_asks() async {
        let clock = TestClock()
        let engine = FakeEngineProbe()
        let recorder = PlaybackProbeRecorder(
            engine: engine,
            route: FixedRoute(route: AudioRoute(
                category: "Playback", mode: "SpokenAudio", policy: "longFormAudio",
                outputs: ["Speaker"]
            )),
            clock: clock
        )
        await recorder.track(episodeId: "ep_1")

        // Paused: not audible, and not a fault.
        await engine.set(EngineFacts(transport: .paused, positionMs: 0))
        var probe = await recorder.sample()
        XCTAssertFalse(probe.isAudible)
        XCTAssertEqual(probe.silenceReason, .notPlaying)

        // Playing, clock moving, audio measured: audible.
        for tick in 0...4 {
            await engine.set(EngineFacts(
                transport: .playing,
                positionMs: tick * 1_000,
                rate: 1,
                level: AudioLevel(rms: 0.08, peak: 0.3, frames: 24_000)
            ))
            _ = await recorder.sample()
            clock.advance(by: 1)
        }
        probe = await recorder.current()
        XCTAssertTrue(probe.isAudible, probe.summary)
        XCTAssertGreaterThan(probe.advancedMs, 0)
        XCTAssertEqual(probe.episodeId, "ep_1")
        let conclusive = await recorder.isConclusive()
        XCTAssertTrue(conclusive)
    }

    func test_a_pause_drops_the_window_rather_than_carrying_it() async {
        let clock = TestClock()
        let engine = FakeEngineProbe()
        let recorder = PlaybackProbeRecorder(engine: engine, clock: clock)
        for tick in 0...4 {
            await engine.set(EngineFacts(transport: .playing, positionMs: tick * 1_000, rate: 1))
            _ = await recorder.sample()
            clock.advance(by: 1)
        }
        await engine.set(EngineFacts(transport: .paused, positionMs: 4_000))
        let probe = await recorder.sample()
        // Without the reset, the pre-pause samples would keep answering "it advanced three
        // seconds" for the width of the window after the audio stopped.
        XCTAssertEqual(probe.advancedMs, 0)
        XCTAssertEqual(probe.silenceReason, .notPlaying)
    }

    func test_the_frozen_player_is_visible_within_the_window() async {
        let clock = TestClock()
        let engine = FakeEngineProbe()
        let recorder = PlaybackProbeRecorder(engine: engine, clock: clock)
        // Exactly the shape of the TestFlight report: told to play, `readyToPlay`, no
        // error, and a clock that never ticks.
        for _ in 0...6 {
            await engine.set(EngineFacts(transport: .playing, positionMs: 9_408, rate: 1))
            _ = await recorder.sample()
            clock.advance(by: 0.5)
        }
        let probe = await recorder.current()
        XCTAssertFalse(probe.isAudible)
        XCTAssertEqual(probe.silenceReason, .clockNotMoving)
        let conclusive = await recorder.isConclusive()
        XCTAssertTrue(conclusive, "the verdict has to be reachable, not merely correct")
    }

    func test_switching_episode_forgets_the_previous_one_positions() async {
        let clock = TestClock()
        let engine = FakeEngineProbe()
        let recorder = PlaybackProbeRecorder(engine: engine, clock: clock)
        await recorder.track(episodeId: "ep_1")
        for tick in 0...3 {
            await engine.set(EngineFacts(transport: .playing, positionMs: 60_000 + tick * 1_000, rate: 1))
            _ = await recorder.sample()
            clock.advance(by: 1)
        }
        await recorder.track(episodeId: "ep_2")
        await engine.set(EngineFacts(transport: .playing, positionMs: 0, rate: 1))
        let probe = await recorder.sample()
        XCTAssertEqual(probe.advancedMs, 0)
        XCTAssertEqual(probe.episodeId, "ep_2")
    }

    func test_a_seek_is_not_listening() async {
        let clock = TestClock()
        let engine = FakeEngineProbe()
        let recorder = PlaybackProbeRecorder(engine: engine, clock: clock)
        await engine.set(EngineFacts(transport: .playing, positionMs: 1_000, rate: 1))
        _ = await recorder.sample()
        clock.advance(by: 1)
        await recorder.discontinuity()
        await engine.set(EngineFacts(transport: .playing, positionMs: 400_000, rate: 1))
        let probe = await recorder.sample()
        XCTAssertEqual(probe.advancedMs, 0, "a jump the listener asked for is not audio")
    }
}

final class BuildEnvironmentTests: XCTestCase {
    func test_an_unlabelled_build_is_production() {
        // The two ways to be wrong are not symmetric: a badge over real data is worse than
        // no badge over staging data.
        XCTAssertEqual(BuildEnvironment(label: nil), .production)
        XCTAssertEqual(BuildEnvironment(label: ""), .production)
        XCTAssertEqual(BuildEnvironment(label: "  "), .production)
        XCTAssertEqual(BuildEnvironment(label: "nonsense"), .production)
    }

    func test_labels_are_read_case_and_whitespace_insensitively() {
        // The value comes through an Xcode build setting and an Info.plist substitution,
        // and either can arrive with a stray space.
        XCTAssertEqual(BuildEnvironment(label: " Staging "), .staging)
        XCTAssertEqual(BuildEnvironment(label: "STAGING"), .staging)
        XCTAssertEqual(BuildEnvironment(label: "development"), .development)
    }

    func test_only_a_non_production_build_announces_itself() {
        XCTAssertNil(BuildEnvironment.production.badge)
        XCTAssertEqual(BuildEnvironment.staging.badge, "STAGING")
        XCTAssertEqual(BuildEnvironment.development.badge, "DEV")
    }

    func test_the_target_reports_a_host_and_where_it_came_from() {
        let target = BuildTarget(
            environment: .staging,
            host: BuildTarget.host(of: URL(string: "https://Example.TEST/v1/")),
            isBuildDefault: true
        )
        XCTAssertEqual(target.host, "example.test")
        XCTAssertEqual(target.summary, "env=staging host=example.test source=build")
    }

    func test_a_server_typed_in_settings_is_not_the_build_default() {
        let target = BuildTarget(environment: .staging, host: nil, isBuildDefault: false)
        XCTAssertEqual(target.summary, "env=staging host=none source=settings")
    }
}
