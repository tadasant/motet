import XCTest

/// **The one flow an agent can drive end to end, and the only place "is sound coming out"
/// is answered by a run rather than by an argument.**
///
/// Everything above this is either compiled and not run (`ios/bin/build-app`) or run and
/// not on a device (`ios/bin/ci-swift`). This boots a simulator, launches the real app,
/// plays real audio through the real `AVPlayer`, and reads the app's own measurement of
/// what came out. It is one flow on purpose: a broad suite that is flaky on day one
/// reddens every iOS pull request for reasons that have nothing to do with the change.
///
/// **What it cannot do, and why the flow is shaped the way it is.** An agent cannot sign
/// in — Google refuses an automated browser at the identifier step, which AGENTS.md
/// records as settled — so no test here reaches a screen that needs a session or an
/// episode that came from a server. It drives `-MotetPlaybackProbe` instead: a Debug-only
/// screen that plays a tone this process generated, through the shipping engine, the
/// shipping audio session and the shipping probe. What that proves is everything between
/// the play button and the audio tap. What it does not touch is the network half.
final class PlaybackProbeUITests: XCTestCase {

    /// Long enough for `PlaybackClockWatch`'s window to fill twice over, and for a cold
    /// simulator to decode the first buffer.
    private let verdictTimeout: TimeInterval = 20

    override func setUp() {
        continueAfterFailure = false
    }

    /// **The whole point of the exercise: the signal changes between playing and not.**
    ///
    /// Asserted in both directions, because only one of them is interesting on its own.
    /// "It says audible while playing" would pass on a probe hard-wired to `true`; "it says
    /// silent while paused" would pass on one hard-wired to `false`. The transition is the
    /// claim.
    func test_the_playback_signal_moves_between_playing_and_paused() throws {
        let app = launchProbe()

        let verdict = app.staticTexts["probe-verdict"]
        XCTAssertTrue(verdict.waitForExistence(timeout: verdictTimeout), "the probe screen never rendered")

        // Before anything is played: silent, and for the honest reason.
        let before = try waitForProbe(app) { $0["transport"] == "paused" }
        XCTAssertEqual(before["audible"], "false", "a player nobody started cannot be audible: \(before)")
        XCTAssertEqual(before["silence"], "notPlaying", "\(before)")
        attach(before, named: "probe-before-play")

        let playPause = app.buttons["probe-play-pause"]
        XCTAssertTrue(playPause.waitForExistence(timeout: verdictTimeout))
        // The button is disabled until the tone is written and loaded; tapping early is a
        // no-op that then fails a long way from the cause.
        XCTAssertTrue(
            waitUntil(timeout: verdictTimeout) { playPause.isEnabled },
            "the probe never finished loading: \(app.staticTexts["probe-load-error"].label)"
        )
        playPause.tap()

        // Playing: the transport says so, the clock is moving, and — where the tap is
        // working — the mix is rendering audio above the silence floor.
        let playing = try waitForProbe(app, timeout: verdictTimeout) {
            $0["transport"] == "playing" && (Int($0["advanced_ms"] ?? "0") ?? 0) > 0
        }
        attach(playing, named: "probe-while-playing")
        XCTAssertEqual(playing["transport"], "playing", "\(playing)")
        XCTAssertGreaterThan(Int(playing["advanced_ms"] ?? "0") ?? 0, 0, "\(playing)")
        XCTAssertEqual(
            playing["silence"], "none",
            "playing with a moving clock has to read as audible: \(playing)"
        )
        XCTAssertEqual(playing["audible"], "true", "\(playing)")

        // Layer 3 only where something measured it. An abstention is recorded rather than
        // failed: `rms=unmeasured` means no tap was installed, which is a different claim
        // from silence and must not be reported as one.
        if let rms = playing["rms"], rms != "unmeasured" {
            XCTAssertEqual(
                playing["sounding"], "true",
                "the audio tap measured the mix and found nothing above the floor: \(playing)"
            )
            XCTAssertGreaterThan(Double(playing["peak"] ?? "0") ?? 0, 0.0005, "\(playing)")
        } else {
            XCTContext.runActivity(named: "audio level unmeasured") { _ in
                // Not a failure: a simulator with no audio device installs the tap and is
                // handed nothing, and layers 1 and 2 still answer the question.
            }
        }

        playPause.tap()

        // Paused: back to silent, and the clock window is dropped rather than carried.
        let paused = try waitForProbe(app, timeout: verdictTimeout) { $0["transport"] == "paused" }
        attach(paused, named: "probe-after-pause")
        XCTAssertEqual(paused["audible"], "false", "\(paused)")
        XCTAssertEqual(paused["advanced_ms"], "0", "a paused player must not keep reporting progress: \(paused)")
        XCTAssertEqual(paused["silence"], "notPlaying", "\(paused)")

        // The transition, stated as the assertion it is.
        XCTAssertNotEqual(
            playing["audible"], paused["audible"],
            "the signal has to distinguish playing from paused, or it is not a signal"
        )
    }

    /// **The staging variant resolves the server it was built with, at runtime.**
    ///
    /// The host is whatever the build was given on the command line — this repository is
    /// public and names no deployment — so what is asserted is that the label and the host
    /// survive the whole path: build setting → `Info.plist` substitution →
    /// `CredentialStore.buildTarget` → the screen.
    ///
    /// **The expectation is passed in and the assertion is conditional, and the reason is
    /// worth knowing rather than papering over.** How a test *runner* process receives an
    /// environment variable differs between `xcodebuild` versions and destinations, so a
    /// test that hard-required one would be red on a toolchain where the variable did not
    /// arrive — which is a failure about plumbing, not about the app. `ios/bin/ui-test`
    /// therefore proves the substitution a second way that needs no plumbing at all: it
    /// reads `MotetBuildEnvironment` and `MotetDefaultBaseURL` out of the built `.app`'s
    /// own `Info.plist`. This half proves what the *running* app made of them, and the
    /// unconditional assertions below stand whether or not the expectation arrived.
    func test_the_build_reports_the_environment_and_server_it_was_built_with() throws {
        let app = launchProbe()
        let line = app.staticTexts["build-target"]
        XCTAssertTrue(line.waitForExistence(timeout: verdictTimeout))
        let fields = parse(line.label)
        attach(fields, named: "build-target")

        // Unconditional: the line has to parse, and it has to say where its server came
        // from. A build whose default was overridden by a saved server is not evidence
        // about the build.
        XCTAssertNotNil(fields["env"], "the build target line did not parse: \(line.label)")
        XCTAssertEqual(
            fields["source"], "build",
            "nothing has been saved under Advanced, so the server must be the build's: \(fields)"
        )

        let environment = ProcessInfo.processInfo.environment
        let expectedEnv = environment["MOTET_UI_TEST_EXPECT_ENV"] ?? ""
        let expectedHost = environment["MOTET_UI_TEST_EXPECT_HOST"] ?? ""
        if expectedEnv.isEmpty, expectedHost.isEmpty {
            XCTContext.runActivity(named: "expectation not supplied to the runner") { _ in
                // `ios/bin/ui-test` asserts the same two values against the built app's
                // Info.plist, so the run is never left with no evidence.
            }
            return
        }
        if !expectedEnv.isEmpty {
            XCTAssertEqual(fields["env"], expectedEnv, "\(fields)")
        }
        if !expectedHost.isEmpty {
            XCTAssertEqual(fields["host"], expectedHost, "\(fields)")
        }
    }

    // MARK: - Helpers

    private func launchProbe() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments = ["-MotetPlaybackProbe"]
        app.launch()
        return app
    }

    /// Poll the probe line until it satisfies `condition`, then answer its parsed fields.
    ///
    /// Polling rather than `expectation(for:)` because the value being waited on is a
    /// *parse* of a label rather than a predicate over an element's own properties, and
    /// because a failure here has to carry the last line it saw — "the probe never became
    /// audible" is not a bug report, and "audible=false transport=playing advanced_ms=0"
    /// is.
    @discardableResult
    private func waitForProbe(
        _ app: XCUIApplication,
        timeout: TimeInterval = 20,
        until condition: ([String: String]) -> Bool
    ) throws -> [String: String] {
        let deadline = Date().addingTimeInterval(timeout)
        var last: [String: String] = [:]
        let element = app.staticTexts["playback-probe"]
        while Date() < deadline {
            if element.exists {
                last = parse(element.label)
                if condition(last) { return last }
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
        attach(last, named: "probe-at-timeout")
        throw ProbeNeverSettled(last: last)
    }

    /// Thrown rather than `XCTFail`ed, so the failure carries the last line the probe
    /// published: "the probe never became audible" is not a bug report, and
    /// "audible=false transport=playing advanced_ms=0" is.
    private struct ProbeNeverSettled: Error, CustomStringConvertible {
        let last: [String: String]
        var description: String {
            let line = last.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }
                .joined(separator: " ")
            return "the playback probe never satisfied the condition; last line: [\(line)]"
        }
    }

    private func waitUntil(timeout: TimeInterval, _ condition: () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            Thread.sleep(forTimeInterval: 0.2)
        }
        return condition()
    }

    /// `key=value key=value` — the shape `PlaybackProbe.summary` and `BuildTarget.summary`
    /// both emit, so one parser reads both.
    private func parse(_ line: String) -> [String: String] {
        var fields: [String: String] = [:]
        for field in line.split(separator: " ") {
            let halves = field.split(separator: "=", maxSplits: 1)
            guard halves.count == 2 else { continue }
            fields[String(halves[0])] = String(halves[1])
        }
        return fields
    }

    /// Put the line in the result bundle, so a red run on a machine nobody here has is
    /// diagnosable from the artifact rather than from a re-run.
    private func attach(_ fields: [String: String], named name: String) {
        let text = fields.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }
            .joined(separator: " ")
        let attachment = XCTAttachment(string: text)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
