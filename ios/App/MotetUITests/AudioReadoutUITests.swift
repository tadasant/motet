import XCTest

/// **The device-farm readout, read the way a farm driver will read it** (motet#152): by
/// accessibility identifier, never by a parse of how it is drawn.
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
        let asking = try waitForReadout(app) { $0[ID.mode] == "live" && $0[ID.transport] == "paused" }
        XCTAssertEqual(asking[ID.verdict], "unmeasured", "a Live pause is not a measured silence: \(asking)")
        XCTAssertEqual(asking[ID.reason], "live_reply_untapped", "\(asking)")
        screenshot(app, "readout-live-asking")

        let resume = app.buttons["probe-live-resume"]
        XCTAssertTrue(waitUntil { resume.isEnabled })
        resume.tap()
        try waitForReadout(app) { $0[ID.mode] == "live" && $0[ID.transport] == "playing" }
    }

    // MARK: - Helpers

    /// The identifiers, spelled here rather than imported: a UI test target cannot see
    /// MotetKit, and a driver on a farm will not either — these strings are the contract
    /// `ios/README.md` documents.
    private enum ID {
        static let mode = "audio-readout-mode"
        static let verdict = "audio-readout-verdict"
        static let reason = "audio-readout-reason"
        static let transport = "audio-readout-transport"
        static let clock = "audio-readout-clock"
        static let rms = "audio-readout-rms"
        static let all = [mode, verdict, reason, transport, clock, rms]
    }

    private func launch(live: Bool) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-MotetPlaybackProbe", "-MotetAudioReadout"] + (live ? ["-MotetPlaybackProbeLive"] : [])
        app.launch()
        XCTAssertTrue(
            app.otherElements["audio-readout"].waitForExistence(timeout: timeout)
                || app.staticTexts[ID.verdict].waitForExistence(timeout: timeout),
            "the readout never rendered"
        )
        return app
    }

    /// Each field's accessibility value, keyed by identifier — what a farm driver reads.
    private func read(_ app: XCUIApplication) -> [String: String] {
        var fields: [String: String] = [:]
        for id in ID.all {
            let element = app.staticTexts[id]
            guard element.exists else { continue }
            fields[id] = (element.value as? String) ?? element.label
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
