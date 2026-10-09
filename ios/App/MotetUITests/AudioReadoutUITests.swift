import XCTest

/// **The device-farm readout, read the way a farm driver will read it** (motet#152): by
/// accessibility identifier. The documented per-field elements must exist, and the values
/// are read off `audio-readout-summary`, the one element that is a single frame.
///
/// Same fixture as `PlaybackProbeUITests` — a generated tone through the shipping player —
/// with `-MotetAudioReadout` on, and for Live mode `-MotetPlaybackProbeLive`, which runs the
/// real `LiveSession` over a scripted socket (`LiveProbeFixture`).
///
/// **The assertion is still the transition**, and in both modes the end that may not move
/// is the one that would be a false claim of sound: paused is `silent`, a session that has
/// paused the narration for a question is `unmeasured`, and neither may ever read
/// `audible`. While playing, `audible` is what a runner with a working tap shows (#140's
/// run measured the tone at rms 0.3535); `unmeasured` is tolerated there for #140's reason —
/// a runner with no audio device installs the tap and is handed nothing — and `silent` is
/// not, because layers 1 and 2 both have to have said yes.
final class AudioReadoutUITests: XCTestCase {
    private let timeout: TimeInterval = 20

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func test_the_listen_readout_reads_audible_playing_and_silent_paused() throws {
        let app = launch(live: false)

        try waitForReadout(app) { $0[ID.mode] == "listen" && $0[ID.transport] == "paused" }
        let idle = try waitForReadout(app) { $0[ID.verdict] == "silent" }
        XCTAssertEqual(idle[ID.clock], "stopped", "\(idle)")
        screenshot(app, "readout-listen-idle")

        let playPause = app.buttons["probe-play-pause"]
        XCTAssertTrue(waitUntil { playPause.isEnabled }, "the probe never finished loading")
        playPause.tap()

        let playing = try waitForReadout(app) {
            $0[ID.transport] == "playing" && $0[ID.clock] == "advancing"
                && ($0[ID.verdict] == "audible" || $0[ID.reason] == "no_tap")
        }
        XCTAssertNotEqual(playing[ID.verdict], "silent", "\(playing)")
        if playing[ID.verdict] == "audible" {
            XCTAssertNotEqual(playing[ID.rms], "unmeasured", "audible without a measurement: \(playing)")
        }
        screenshot(app, "readout-listen-playing")

        playPause.tap()
        let paused = try waitForReadout(app) { $0[ID.transport] == "paused" }
        XCTAssertEqual(paused[ID.verdict], "silent", "\(paused)")
        XCTAssertEqual(paused[ID.mode], "listen", "\(paused)")
        XCTAssertNotEqual(playing[ID.verdict], paused[ID.verdict], "the readout has to move")
        screenshot(app, "readout-listen-paused")
    }

    func test_the_live_readout_reads_audible_narrating_and_unmeasured_asking() throws {
        let app = launch(live: true)

        let start = app.buttons["probe-live-start-stop"]
        XCTAssertTrue(start.waitForExistence(timeout: timeout))
        XCTAssertTrue(waitUntil { start.isEnabled }, "the probe never finished loading")
        start.tap()

        // The session's first `ready` starts the narration under it.
        let narrating = try waitForReadout(app) {
            $0[ID.mode] == "live" && $0[ID.transport] == "playing" && $0[ID.clock] == "advancing"
                && ($0[ID.verdict] == "audible" || $0[ID.verdict] == "unmeasured")
        }
        XCTAssertNotEqual(narrating[ID.verdict], "silent", "\(narrating)")
        screenshot(app, "readout-live-narrating")

        let ask = app.buttons["probe-live-ask"]
        XCTAssertTrue(waitUntil { ask.isEnabled }, "the session never reached narrating")
        ask.tap()

        // The session pauses the narration for the question; the reply path is untapped.
        // On the session's phase, not the transport: the player pauses an instant before the
        // session records that it is listening, and that instant is neither state.
        let asking = try waitForReadout(app) {
            $0[ID.mode] == "live" && $0["live_phase"] == "listening" && $0[ID.transport] == "paused"
        }
        XCTAssertEqual(asking[ID.verdict], "unmeasured", "a Live pause is not a measured silence: \(asking)")
        XCTAssertEqual(asking[ID.reason], "live_reply_untapped", "\(asking)")
        screenshot(app, "readout-live-asking")

        let resume = app.buttons["probe-live-resume"]
        XCTAssertTrue(waitUntil { resume.isEnabled })
        resume.tap()
        try waitForReadout(app) { $0[ID.mode] == "live" && $0[ID.transport] == "playing" }
    }

    // MARK: - Helpers

    /// The summary's keys. The fields are asserted off `audio-readout-summary` rather than
    /// element by element, because the readout re-renders twice a second and eight separate
    /// queries can straddle an update — a verdict from one frame beside a transport from the
    /// next. One element is one frame.
    private enum ID {
        static let mode = "mode"
        static let verdict = "verdict"
        static let reason = "reason"
        static let transport = "transport"
        static let clock = "clock"
        static let rms = "rms"
    }

    /// The per-field identifiers a farm driver reads, spelled here rather than imported: a UI
    /// test target cannot see MotetKit, and a driver on a farm will not either — these strings
    /// are the contract `ios/README.md` documents.
    private static let fieldIdentifiers = [
        "audio-readout-mode", "audio-readout-verdict", "audio-readout-reason", "audio-readout-transport",
        "audio-readout-position", "audio-readout-clock", "audio-readout-rms", "audio-readout-route",
    ]

    private func launch(live: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-MotetPlaybackProbe", "-MotetAudioReadout"] + (live ? ["-MotetPlaybackProbeLive"] : [])
        app.launch()
        XCTAssertTrue(
            app.staticTexts["audio-readout-summary"].waitForExistence(timeout: timeout),
            "the readout never rendered"
        )
        for id in Self.fieldIdentifiers {
            XCTAssertTrue(app.staticTexts[id].exists, "no element carries the documented identifier \(id)")
        }
        return app
    }

    /// The summary line's `key=value` fields — one frame of the readout.
    private func read(_ app: XCUIApplication) -> [String: String] {
        let element = app.staticTexts["audio-readout-summary"]
        guard element.exists else { return [:] }
        let line = (element.value as? String) ?? element.label
        var fields: [String: String] = [:]
        for field in line.split(separator: " ") {
            let halves = field.split(separator: "=", maxSplits: 1)
            guard halves.count == 2 else { continue }
            fields[String(halves[0])] = String(halves[1])
        }
        return fields
    }

    @discardableResult
    private func waitForReadout(
        _ app: XCUIApplication, until condition: ([String: String]) -> Bool
    ) throws -> [String: String] {
        let deadline = Date().addingTimeInterval(timeout)
        var last: [String: String] = [:]
        while Date() < deadline {
            last = read(app)
            if condition(last) { return last }
            Thread.sleep(forTimeInterval: 0.25)
        }
        screenshot(app, "readout-at-timeout")
        throw ReadoutNeverSettled(last: last)
    }

    private struct ReadoutNeverSettled: Error, CustomStringConvertible {
        let last: [String: String]
        var description: String {
            "the audio readout never satisfied the condition; last: "
                + last.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: " ")
        }
    }

    private func waitUntil(_ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            Thread.sleep(forTimeInterval: 0.2)
        }
        return condition()
    }

    private func screenshot(_ app: XCUIApplication, _ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
