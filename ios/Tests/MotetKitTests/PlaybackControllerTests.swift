import XCTest
@testable import MotetKit

/// The player's rules: deterministic commands, our position, read state that follows.
final class PlaybackControllerTests: XCTestCase {
    private struct Harness {
        let engine: ScriptedEngine
        let controller: PlaybackController
        let api: FakeAPI
        let outbox: Outbox
        let positions: ListeningPositionStore
        let clock: TestClock
    }

    private func makeHarness(
        store: InMemoryKeyValueStore = InMemoryKeyValueStore(),
        settings: PlaybackSettings = PlaybackSettings()
    ) async -> Harness {
        let clock = TestClock()
        let engine = ScriptedEngine()
        let api = FakeAPI()
        let outbox = Outbox(store: store, clock: clock)
        let positions = ListeningPositionStore(store: store, clock: clock)
        let readState = ReadStateCoordinator(api: api, outbox: outbox)
        let controller = PlaybackController(
            engine: engine, positions: positions, readState: readState,
            settings: settings, clock: clock
        )
        await controller.activate()
        return Harness(
            engine: engine, controller: controller, api: api,
            outbox: outbox, positions: positions, clock: clock
        )
    }

    private func load(_ harness: Harness, episode: EpisodeResponse = Fixture.episode()) async throws {
        try await harness.controller.load(
            episode: episode,
            source: PlaybackController.Source(url: URL(string: "file:///tmp/ep.mp3")!, isLocal: true),
            autoplay: true
        )
    }

    // MARK: - Deterministic commands

    func testSkipsUseTheConfiguredIntervals() async throws {
        let harness = await makeHarness(settings: PlaybackSettings(skipForwardMs: 30_000, skipBackwardMs: 15_000))
        try await load(harness)

        await harness.controller.perform(.skipForward)
        let value1 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value1, 30_000)

        await harness.controller.perform(.skipBackward)
        let value2 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value2, 15_000)
    }

    func testSkipsClampToTheEpisodeRatherThanRunningOffTheEnd() async throws {
        let harness = await makeHarness()
        try await load(harness)

        await harness.controller.perform(.seek(toMs: 299_000))
        await harness.controller.perform(.skipForward)
        let value3 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value3, 300_000)

        await harness.controller.perform(.seek(toMs: 2_000))
        await harness.controller.perform(.skipBackward)
        let value4 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value4, 0)
    }

    func testStorySkipsLandOnSegmentBoundaries() async throws {
        let harness = await makeHarness()
        try await load(harness)

        await harness.controller.perform(.nextSegment)
        let value5 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value5, 60_000)
        await harness.controller.perform(.nextSegment)
        let value6 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value6, 90_000)
        await harness.controller.perform(.previousSegment)
        let value7 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value7, 60_000)
    }

    func testSpeedCyclesThroughTheLadderAndReachesTheEngine() async throws {
        let harness = await makeHarness()
        try await load(harness)

        await harness.controller.perform(.setRate(1.5))
        let value8 = await harness.engine.rate
        XCTAssertEqual(value8, 1.5)
        await harness.controller.perform(.cycleRate)
        let value9 = await harness.controller.snapshot().rate
        XCTAssertEqual(value9, 1.75)
    }

    func testRateIsClampedToWhatThePlayerReproduces() async throws {
        let harness = await makeHarness()
        try await load(harness)
        await harness.controller.perform(.setRate(99))
        let value10 = await harness.controller.snapshot().rate
        XCTAssertEqual(value10, PlaybackSettings.rateRange.upperBound)
    }

    func testCommandsBeforeAnythingIsLoadedAreIgnored() async throws {
        let harness = await makeHarness()
        await harness.controller.perform(.play)
        let value11 = await harness.engine.isPlaying
        XCTAssertFalse(value11)
    }

    // MARK: - Position ownership (invariant 4)

    func testPlaybackResumesWhereItWasLeft() async throws {
        let store = InMemoryKeyValueStore()
        let first = await makeHarness(store: store)
        try await load(first)
        await first.engine.advance(toMs: 123_000)
        await first.controller.perform(.pause)

        // Relaunch: a brand new controller over the same storage.
        let second = await makeHarness(store: store)
        try await load(second)
        let loaded = await second.engine.loaded
        XCTAssertEqual(loaded?.startingAtMs, 123_000)
        let value12 = await second.controller.snapshot().positionMs
        XCTAssertEqual(value12, 123_000)
    }

    func testAnInterruptionKeepsOurPositionRatherThanThePlayers() async throws {
        let harness = await makeHarness()
        try await load(harness)
        await harness.engine.advance(toMs: 45_000)

        await harness.engine.interrupt(resumable: true)

        // The engine is stopped and would report whatever it likes; we keep 45s.
        let value13 = await harness.controller.snapshot().positionMs
        XCTAssertEqual(value13, 45_000)
        let value14 = await harness.controller.snapshot().isPlaying
        XCTAssertFalse(value14)
        let stored = try await harness.positions.position(for: "ep-1")
        XCTAssertEqual(stored?.spokenThroughMs, 45_000)

        await harness.controller.resumeAfterInterruptionIfNeeded()
        let value15 = await harness.controller.snapshot().isPlaying
        XCTAssertTrue(value15)
    }

    func testANonResumableInterruptionDoesNotStartPlayingBehindTheListenersBack() async throws {
        let harness = await makeHarness()
        try await load(harness)
        await harness.engine.interrupt(resumable: false)
        await harness.controller.resumeAfterInterruptionIfNeeded()
        let value16 = await harness.controller.snapshot().isPlaying
        XCTAssertFalse(value16)
    }

    func testAFinishedEpisodeStartsAgainFromTheTop() async throws {
        let store = InMemoryKeyValueStore()
        let first = await makeHarness(store: store)
        try await load(first)
        await first.engine.finish()

        let second = await makeHarness(store: store)
        try await load(second)
        let value17 = await second.engine.loaded?.startingAtMs
        XCTAssertEqual(value17, 0)
    }

    // MARK: - Read state (invariant 5)

    func testListeningPastAStoryMarksItRead() async throws {
        let harness = await makeHarness()
        try await load(harness)

        await harness.engine.listen(toMs: 60_000)
        let halfway = await harness.api.recordedCalls().filter { $0.name == "setNewsItemRead" }
        XCTAssertEqual(halfway.count, 0, "half of a two-segment story is not the story")

        await harness.engine.listen(toMs: 90_000)
        let calls = await harness.api.recordedCalls().filter { $0.name == "setNewsItemRead" }
        XCTAssertEqual(calls.map(\.detail), ["news-a:true"])
    }

    func testAStoryIsOnlyMarkedOnceHoweverManyTicksCrossIt() async throws {
        let harness = await makeHarness()
        try await load(harness)
        await harness.engine.listen(toMs: 90_000)
        await harness.engine.advance(toMs: 91_000)
        await harness.engine.advance(toMs: 92_000)

        let calls = await harness.api.recordedCalls().filter { $0.name == "setNewsItemRead" }
        XCTAssertEqual(calls.count, 1)
    }

    func testScrubbingBackDoesNotUnmarkAnythingAndDoesNotResend() async throws {
        let harness = await makeHarness()
        try await load(harness)
        await harness.engine.listen(toMs: 90_000)
        await harness.controller.perform(.seek(toMs: 0))
        await harness.engine.listen(toMs: 30_000)

        let calls = await harness.api.recordedCalls().filter { $0.name == "setNewsItemRead" }
        XCTAssertEqual(calls.map(\.detail), ["news-a:true"])
    }

    func testReachingTheEndMarksTheEpisodeListened() async throws {
        let harness = await makeHarness()
        try await load(harness)
        await harness.engine.listen(toMs: 299_000, stepMs: 5_000)
        await harness.engine.finish()

        let names = await harness.api.recordedCalls().map(\.name)
        XCTAssertTrue(names.contains("markEpisodeListened"))
        let stored = try await harness.positions.position(for: "ep-1")
        XCTAssertEqual(stored?.isFinished, true)
        XCTAssertEqual(stored?.spokenThroughMs, 300_000)
    }

    func testAStoryHeardWithNoSignalIsQueuedAndSentLater() async throws {
        let harness = await makeHarness()
        await harness.api.setFailure(.offline)
        try await load(harness)

        await harness.engine.listen(toMs: 90_000)
        let queued = try await harness.outbox.pending()
        XCTAssertEqual(queued.count, 1, "held, not lost")

        await harness.api.setFailure(nil)
        harness.clock.advance(by: 60)
        _ = try await harness.outbox.drain(using: harness.api)

        let sent = await harness.api.successfulCalls().filter {
            $0.name == "setNewsItemRead" && $0.detail == "news-a:true"
        }
        XCTAssertEqual(sent.count, 1)
        let value18 = try await harness.outbox.pending().isEmpty
        XCTAssertTrue(value18)
    }

    func testAlreadyHeardStoriesAreNotReMarkedOnRelaunch() async throws {
        let store = InMemoryKeyValueStore()
        let first = await makeHarness(store: store)
        try await load(first)
        await first.engine.listen(toMs: 90_000)

        let second = await makeHarness(store: store)
        try await load(second)
        await second.engine.listen(toMs: 91_000)

        let calls = await second.api.recordedCalls().filter { $0.name == "setNewsItemRead" }
        XCTAssertTrue(calls.isEmpty, "the previous run already reported it")
    }

    // MARK: - Failures

    func testALoadFailureSurfacesRatherThanLookingLikeSilence() async throws {
        let harness = await makeHarness()
        await harness.engine.setLoadError(MotetError.offline)
        do {
            try await load(harness)
            XCTFail("expected a failure")
        } catch {
            let snapshot = await harness.controller.snapshot()
            XCTAssertNotNil(snapshot.errorMessage)
            XCTAssertFalse(snapshot.isLoading)
        }
    }

    func testSnapshotsReachSubscribers() async throws {
        let harness = await makeHarness()
        var iterator = await harness.controller.snapshots().makeAsyncIterator()
        _ = await iterator.next()  // the initial value
        try await load(harness)
        await harness.engine.advance(toMs: 5_000)

        var seen: PlaybackSnapshot?
        while let next = await iterator.next() {
            seen = next
            if next.positionMs == 5_000 { break }
        }
        XCTAssertEqual(seen?.positionMs, 5_000)
        XCTAssertEqual(seen?.currentSegmentTitle, "Alpha")
    }
}

/// The distinction between *the playhead moved* and *the listener heard it*.
///
/// Every case here was a real defect found in review: the engine echoes a `.position` after
/// a seek, so a high-water mark treated a skip as listening and emptied the backlog.
final class SkippingDoesNotCountAsListeningTests: XCTestCase {
    private func makeHarness(
        store: InMemoryKeyValueStore = InMemoryKeyValueStore()
    ) async -> (engine: ScriptedEngine, controller: PlaybackController, api: FakeAPI,
                positions: ListeningPositionStore) {
        let clock = TestClock()
        let engine = ScriptedEngine()
        let api = FakeAPI()
        let outbox = Outbox(store: store, clock: clock)
        let positions = ListeningPositionStore(store: store, clock: clock)
        let controller = PlaybackController(
            engine: engine,
            positions: positions,
            readState: ReadStateCoordinator(api: api, outbox: outbox),
            clock: clock
        )
        await controller.activate()
        try? await controller.load(
            episode: Fixture.episode(),
            source: PlaybackController.Source(url: URL(string: "file:///tmp/ep.mp3")!, isLocal: true),
            autoplay: true
        )
        return (engine, controller, api, positions)
    }

    private func readCalls(_ api: FakeAPI) async -> [String] {
        await api.recordedCalls().filter { $0.name == "setNewsItemRead" }.map(\.detail)
    }

    func testSkippingPastAStoryDoesNotMarkItRead() async throws {
        let harness = await makeHarness()

        // Two taps of "next story" land at 90s — past the end of Alpha, which was never
        // spoken. The engine echoes the seek as a position report, exactly as AVPlayer does.
        await harness.controller.perform(.nextSegment)
        await harness.controller.perform(.nextSegment)
        await harness.engine.listen(toMs: 95_000)

        let calls = await readCalls(harness.api)
        XCTAssertEqual(calls, [], "a story nobody heard must stay in the backlog")
    }

    func testHoldingSkipForwardThroughAnEpisodeMarksNothing() async throws {
        let harness = await makeHarness()
        for _ in 0..<10 {
            await harness.controller.perform(.skipForward)
        }
        let calls = await readCalls(harness.api)
        XCTAssertEqual(calls, [])
    }

    func testScrubbingToTheEndAndLettingItRunOutMarksOnlyWhatWasHeard() async throws {
        let harness = await makeHarness()

        // Jump to the last five seconds of a five-minute episode and let the file finish.
        await harness.controller.perform(.seek(toMs: 295_000))
        await harness.engine.listen(toMs: 300_000)
        await harness.engine.finish()

        let calls = await readCalls(harness.api)
        XCTAssertEqual(calls, [], "five seconds of Charlie is not three stories")
        let names = await harness.api.recordedCalls().map(\.name)
        XCTAssertFalse(
            names.contains("markEpisodeListened"),
            "'listened' marks every item in the episode read; it has to mean it"
        )
    }

    func testAnEpisodeHeardAllTheWayThroughStillMarksEverything() async throws {
        let harness = await makeHarness()
        await harness.engine.listen(toMs: 300_000, stepMs: 5_000)
        await harness.engine.finish()

        let calls = await readCalls(harness.api)
        XCTAssertEqual(calls, ["news-a:true", "news-b:true", "news-c:true"])
        let names = await harness.api.recordedCalls().map(\.name)
        XCTAssertTrue(names.contains("markEpisodeListened"))
    }

    func testASkippedStoryIsStillUnreadAfterARelaunch() async throws {
        let store = InMemoryKeyValueStore()
        let first = await makeHarness(store: store)
        await first.controller.perform(.seek(toMs: 90_000))
        await first.engine.listen(toMs: 180_000, stepMs: 5_000)
        await first.controller.perform(.pause)

        // What was heard is persisted alongside the position, so the next launch does not
        // fall back to "everything before the furthest point".
        let second = await makeHarness(store: store)
        await second.engine.listen(toMs: 185_000)

        let calls = await readCalls(second.api)
        XCTAssertFalse(calls.contains("news-a:true"), "Alpha was skipped, in both sessions")
    }

    func testPausedTicksDoNotAccumulateListening() async throws {
        let harness = await makeHarness()
        await harness.controller.perform(.pause)
        await harness.engine.listen(toMs: 95_000)
        let calls = await readCalls(harness.api)
        XCTAssertEqual(calls, [])
    }
}

/// `ListenedCoverage` on its own: merging, partial cover, tolerance.
final class ListenedCoverageTests: XCTestCase {
    func testAdjacentAndOverlappingRangesMerge() {
        var coverage = ListenedCoverage()
        coverage.add(from: 0, to: 1_000)
        coverage.add(from: 1_000, to: 2_000)
        coverage.add(from: 1_500, to: 2_500)
        XCTAssertEqual(coverage.ranges, [0..<2_500])
    }

    func testAGapIsKept() {
        var coverage = ListenedCoverage()
        coverage.add(from: 0, to: 1_000)
        coverage.add(from: 5_000, to: 6_000)
        XCTAssertEqual(coverage.ranges, [0..<1_000, 5_000..<6_000])
        XCTAssertFalse(coverage.covers(0..<6_000, tolerance: 500))
        XCTAssertTrue(coverage.covers(0..<1_000, tolerance: 0))
    }

    func testOutOfOrderAdditionsStillMerge() {
        var coverage = ListenedCoverage()
        coverage.add(from: 5_000, to: 6_000)
        coverage.add(from: 0, to: 1_000)
        coverage.add(from: 1_000, to: 5_000)
        XCTAssertEqual(coverage.ranges, [0..<6_000])
    }

    func testToleranceAbsorbsAPlayerThatStopsJustShort() {
        var coverage = ListenedCoverage()
        coverage.add(from: 0, to: 59_700)
        XCTAssertTrue(coverage.covers(0..<60_000, tolerance: 500))
        XCTAssertFalse(coverage.covers(0..<60_000, tolerance: 100))
    }

    func testEmptyAndReversedAdditionsAreHarmless() {
        var coverage = ListenedCoverage()
        coverage.add(from: 100, to: 100)
        coverage.add(from: -50, to: -10)
        XCTAssertTrue(coverage.isEmpty)
        coverage.add(from: 900, to: 300)
        XCTAssertEqual(coverage.ranges, [300..<900], "a reversed pair is still an interval")
    }
}
