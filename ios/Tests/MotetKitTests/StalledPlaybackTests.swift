import XCTest
@testable import MotetKit

/// The state that had no name: the player was told to play and no audio came out.
///
/// Reported against the TestFlight build on 2026-09-20 — "the Play button doesn't actually
/// start the audio… the timer line doesn't move". Production telemetry showed the phone
/// fetching the episode audio and the API answering 307, so nothing was wrong on the
/// server and nothing was wrong with the request; `AVPlayer` simply sat in
/// `waitingToPlayAtSpecifiedRate`, which this app observed nowhere. `AVPlayerItem.status`
/// stays `.readyToPlay` throughout, no error is ever set, and the clock does not tick — so
/// every other observation in the engine reported that all was well.
///
/// These are the rules that turn that silence into something the screen can say. They run
/// against the scripted engine, on Linux, because the *decision* is the part that can be
/// wrong; whether `AVPlayer` reaches that state is a device fact (`ios/README.md`).
final class StalledPlaybackTests: XCTestCase {
    private struct Harness {
        let engine: ScriptedEngine
        let controller: PlaybackController
        let clock: TestClock
    }

    private func makeHarness() async throws -> Harness {
        let store = InMemoryKeyValueStore()
        let clock = TestClock()
        let engine = ScriptedEngine()
        let controller = PlaybackController(
            engine: engine,
            positions: ListeningPositionStore(store: store, clock: clock),
            readState: ReadStateCoordinator(api: FakeAPI(), outbox: Outbox(store: store, clock: clock)),
            clock: clock
        )
        await controller.activate()
        // The failing shape: the engine accepts `play` and never announces playing.
        await engine.setAnnouncesPlaying(false)
        try await controller.load(
            episode: Fixture.episode(),
            source: PlaybackController.Source(url: URL(string: "https://api.example/audio")!, isLocal: false),
            autoplay: true
        )
        return Harness(engine: engine, controller: controller, clock: clock)
    }

    // MARK: - While it is still plausibly temporary

    func testABufferingWaitIsSaidOnScreenRatherThanLookingLikeAWorkingPlayer() async throws {
        let harness = try await makeHarness()

        await harness.engine.waiting(.toMinimizeStalls)

        let snapshot = await harness.controller.snapshot()
        XCTAssertEqual(snapshot.stallMessage, PlaybackWaitReason.toMinimizeStalls.sentence)
        // Still a hopeful state, not a failure: the spinner stays and no error is claimed.
        XCTAssertNil(snapshot.errorMessage)
        XCTAssertTrue(snapshot.isLoading)
    }

    func testAWaitThatClearsLeavesNothingBehind() async throws {
        let harness = try await makeHarness()
        await harness.engine.waiting(.evaluatingBufferingRate)

        await harness.engine.waiting(nil)

        let snapshot = await harness.controller.snapshot()
        XCTAssertNil(snapshot.stallMessage)
        XCTAssertNil(snapshot.errorMessage)
        XCTAssertFalse(snapshot.isLoading)
    }

    func testAClockThatMovesOutranksAWaitHoweverTheEventsArrived() async throws {
        let harness = try await makeHarness()
        await harness.engine.waiting(.toMinimizeStalls)

        // The engine hands events to unordered `Task`s, so a `waiting` can land after the
        // position that disproves it. Forward progress is the unarguable evidence.
        await harness.engine.advance(toMs: 1_000)

        let snapshot = await harness.controller.snapshot()
        XCTAssertNil(snapshot.stallMessage)
        XCTAssertNil(snapshot.errorMessage)
    }

    // MARK: - When it is not temporary

    func testAWaitThatOutlastsItsGraceBecomesAnErrorTheScreenCanShow() async throws {
        let harness = try await makeHarness()

        await harness.engine.waiting(.toMinimizeStalls)
        harness.clock.advance(by: PlaybackController.stallGraceSeconds + 1)
        // The engine re-sends the same reason while the wait lasts; that heartbeat is what
        // the decision hangs on, because a frozen clock emits nothing else.
        await harness.engine.waiting(.toMinimizeStalls)

        let snapshot = await harness.controller.snapshot()
        XCTAssertEqual(snapshot.errorMessage, PlaybackWaitReason.toMinimizeStalls.failureSentence)
        // One sentence at a time: the hopeful one goes when the failing one arrives.
        XCTAssertNil(snapshot.stallMessage)
        XCTAssertFalse(snapshot.isLoading)
    }

    func testNothingToPlayIsAFailureAtOnceRatherThanAfterTheGrace() async throws {
        let harness = try await makeHarness()

        await harness.engine.waiting(.noItemToPlay)

        let snapshot = await harness.controller.snapshot()
        // Nothing is arriving that would end this wait, so waiting fifteen seconds to say
        // so is fifteen seconds of a listener staring at a button they already pressed.
        XCTAssertFalse(PlaybackWaitReason.noItemToPlay.clearsItself)
        XCTAssertEqual(snapshot.errorMessage, PlaybackWaitReason.noItemToPlay.failureSentence)
    }

    func testAWaitOnAPlayerNobodyAskedToPlayIsNotNarrated() async throws {
        let harness = try await makeHarness()
        await harness.controller.perform(.pause)

        await harness.engine.waiting(.toMinimizeStalls)
        harness.clock.advance(by: PlaybackController.stallGraceSeconds + 1)
        await harness.engine.waiting(.toMinimizeStalls)

        // `AVPlayer` reports this state while pre-rolling a paused item too. A stall nobody
        // is waiting for is its own kind of lying.
        let snapshot = await harness.controller.snapshot()
        XCTAssertNil(snapshot.stallMessage)
        XCTAssertNil(snapshot.errorMessage)
    }

    func testAskingAgainClearsTheLastFailure() async throws {
        let harness = try await makeHarness()
        await harness.engine.waiting(.noItemToPlay)
        let failed = await harness.controller.snapshot().errorMessage
        XCTAssertNotNil(failed)

        await harness.controller.perform(.play)

        // "Try again" is exactly this command; what is on screen next must be about this
        // attempt rather than the last one.
        let snapshot = await harness.controller.snapshot()
        XCTAssertNil(snapshot.errorMessage)
        XCTAssertNil(snapshot.stallMessage)
    }

    func testPausingAfterAStallKeepsTheReasonOnScreen() async throws {
        let harness = try await makeHarness()
        await harness.engine.waiting(.noItemToPlay)

        // The double sends `.waiting(nil)` *and* `.paused`, as the real engine does, because
        // the fix is about which of them wins: the answer to "why did nothing happen" must
        // survive the listener's next tap whichever order they arrive in.
        await harness.controller.perform(.pause)

        let snapshot = await harness.controller.snapshot()
        XCTAssertEqual(snapshot.errorMessage, PlaybackWaitReason.noItemToPlay.failureSentence)
    }

    func testAWaitThatEndsOnItsOwnDoesNotWipeAFailureThePauseAlreadyRaised() async throws {
        let harness = try await makeHarness()
        await harness.engine.waiting(.noItemToPlay)

        // `waiting(nil)` alone — the half of a pause that used to arrive first and clear it.
        await harness.engine.waiting(nil)

        let snapshot = await harness.controller.snapshot()
        XCTAssertEqual(snapshot.errorMessage, PlaybackWaitReason.noItemToPlay.failureSentence)
    }

    func testAWaitThatEndsBecauseAudioStartedDoesClearTheFailure() async throws {
        let harness = try await makeHarness()
        await harness.engine.waiting(.noItemToPlay)

        // The other side of the bargain: `waiting(nil)` keeps the error, so something else
        // has to take it away when the audio genuinely arrives. Both of these do.
        await harness.engine.waiting(nil)
        await harness.engine.advance(toMs: 1_000)
        let snapshot = await harness.controller.snapshot()
        XCTAssertNil(snapshot.errorMessage)
    }

    func testTheGraceIsMeasuredFromTheStartOfTheWaitNotOfTheCurrentReason() async throws {
        let harness = try await makeHarness()

        // `AVPlayer` walks between reasons inside one wait as a matter of routine. Restarting
        // the clock on each turned a promised 15 s into 45, and into never for one that flaps.
        await harness.engine.waiting(.evaluatingBufferingRate)
        harness.clock.advance(by: 8)
        await harness.engine.waiting(.toMinimizeStalls)
        harness.clock.advance(by: 8)
        await harness.engine.waiting(.toMinimizeStalls)

        let snapshot = await harness.controller.snapshot()
        XCTAssertEqual(snapshot.errorMessage, PlaybackWaitReason.toMinimizeStalls.failureSentence)
    }

    func testAPlayerThatNeverLeavesPausedIsReportedRatherThanLookingLikePlayback() async throws {
        let harness = try await makeHarness()

        // The reported bug's own shape: `playImmediately` left the player `.paused`, so
        // `timeControlStatus` never changed and no AVFoundation reason exists to read. The
        // engine's watchdog reports `.notStarted`; the controller treats it like any other
        // wait that can clear, so it is a sentence first and an error after the grace.
        await harness.engine.waiting(.notStarted)
        let waiting = await harness.controller.snapshot()
        XCTAssertEqual(waiting.stallMessage, PlaybackWaitReason.notStarted.sentence)

        harness.clock.advance(by: PlaybackController.stallGraceSeconds + 1)
        await harness.engine.waiting(.notStarted)
        let failed = await harness.controller.snapshot()
        XCTAssertEqual(failed.errorMessage, PlaybackWaitReason.notStarted.failureSentence)
    }

    // MARK: - The session, which is why *any* episode would be silent

    func testARefusedAudioSessionIsCarriedOnTheSnapshotTheScreenReads() async throws {
        let harness = try await makeHarness()

        await harness.controller.report(audioSessionMessage: AudioSessionPlan.noShapeAccepted)

        let snapshot = await harness.controller.snapshot()
        XCTAssertEqual(snapshot.audioSessionMessage, AudioSessionPlan.noShapeAccepted)

        await harness.controller.report(audioSessionMessage: nil)
        let cleared = await harness.controller.snapshot().audioSessionMessage
        XCTAssertNil(cleared)
    }

    func testEveryWaitingReasonHasASentenceOfItsOwn() {
        // A reason that fell back to a shared string would be a reason nobody could act on,
        // and `unknown` exists so that a future iOS adding one is still reported.
        let sentences = Set(PlaybackWaitReason.allCases.map(\.sentence))
        XCTAssertEqual(sentences.count, PlaybackWaitReason.allCases.count)
        for reason in PlaybackWaitReason.allCases {
            XCTAssertFalse(reason.failureSentence.isEmpty)
        }
    }
}

/// The ladder of audio-session shapes, which is the other half of "I can't hear anything".
///
/// A refused `setCategory` used to be swallowed by `try?` at both call sites, leaving the
/// app on `.soloAmbient` — muted by the ringer switch, stopped on lock — with nothing said.
final class AudioSessionPlanTests: XCTestCase {
    func testEveryRungKeepsPlaybackAndTheFirstGivesUpNothing() {
        let ladder = AudioSessionPlan.listening
        XCTAssertFalse(ladder.isEmpty)
        XCTAssertEqual(ladder.first?.mode, .spokenAudio)
        XCTAssertEqual(ladder.first?.policy, .longFormAudio)
        XCTAssertTrue(ladder[0].concession.isEmpty, "the best rung gives up nothing")
    }

    func testEveryLaterRungSaysWhatItGivesUp() {
        for shape in AudioSessionPlan.listening.dropFirst() {
            XCTAssertFalse(shape.concession.isEmpty, "\(shape.label) must say what it costs")
            XCTAssertTrue(AudioSessionPlan.Outcome.configured(shape).logLine.contains(shape.concession))
        }
    }

    // MARK: - Walking it

    func testTheFirstRungThatIsAcceptedWins() {
        var asked: [AudioSessionShape] = []
        let outcome = AudioSessionPlan.walk { asked.append($0) }
        XCTAssertEqual(outcome, .configured(AudioSessionPlan.listening[0]))
        XCTAssertEqual(asked, [AudioSessionPlan.listening[0]], "a rung that took must end the walk")
    }

    func testARefusedRungFallsThroughToTheNextOne() {
        var asked: [AudioSessionShape] = []
        let outcome = AudioSessionPlan.walk { shape in
            asked.append(shape)
            if shape.policy == .longFormAudio { throw Refusal() }
        }
        XCTAssertEqual(outcome, .configured(AudioSessionPlan.listening[1]))
        XCTAssertEqual(asked.count, 2)
    }

    func testRunningOutOfRungsKeepsEveryRefusalAndTellsTheListener() {
        let outcome = AudioSessionPlan.walk { _ in throw Refusal() }
        guard case .refused(let errors) = outcome else { return XCTFail("expected a refusal") }
        XCTAssertEqual(errors.count, AudioSessionPlan.listening.count, "every rung's error is kept")
        for shape in AudioSessionPlan.listening {
            XCTAssertTrue(errors.contains { $0.hasPrefix(shape.label) }, "\(shape.label) is named")
        }
        XCTAssertEqual(outcome.listenerMessage, AudioSessionPlan.noShapeAccepted)
        XCTAssertTrue(outcome.isRefusal)
    }

    func testAWorkingFallbackIsNotShownToTheListener() {
        // A lower rung that took is a player that works. Putting "no AirPlay 2 grouping" on
        // the player screen in error red would train the listener to ignore the one line
        // that means the audio will be silent, so the concession goes to the log alone.
        for shape in AudioSessionPlan.listening {
            XCTAssertNil(AudioSessionPlan.Outcome.configured(shape).listenerMessage)
            XCTAssertFalse(AudioSessionPlan.Outcome.configured(shape).isRefusal)
        }
    }

    private struct Refusal: Error {}

    func testTheLadderEndsSomewhereEveryPhoneShouldAccept() {
        // The last rung is the plainest `.playback` there is. If iOS refuses even that, the
        // answer is the sentence below rather than another rung.
        XCTAssertEqual(AudioSessionPlan.listening.last?.mode, .standard)
        XCTAssertEqual(AudioSessionPlan.listening.last?.policy, .standard)
        XCTAssertTrue(AudioSessionPlan.noShapeAccepted.contains("ringer switch"))
    }

    func testNoTwoRungsAskIOSForTheSameThing() {
        // On `(mode, policy)` and not on the shape, whose synthesised `Hashable` includes
        // the prose: two rungs asking for an identical session would otherwise pass as
        // "distinct" merely because they describe themselves differently.
        let asked = AudioSessionPlan.listening.map { "\($0.mode)/\($0.policy)" }
        XCTAssertEqual(Set(asked).count, asked.count)
    }
}
