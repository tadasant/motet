import XCTest
@testable import MotetKit

/// The position is cross-device (motet#11): the phone reads the server's
/// `listened_through_ms` when it loads an episode and writes its own listening frontier back,
/// by the SPA's rule — only continuous listening from the frontier moves it.
final class ServerPositionTests: XCTestCase {
    private struct Harness {
        let engine: ScriptedEngine
        let controller: PlaybackController
        let api: FakeAPI
        let positions: ListeningPositionStore
        let clock: TestClock
    }

    private func makeHarness(store: InMemoryKeyValueStore = InMemoryKeyValueStore()) async -> Harness {
        let clock = TestClock()
        let engine = ScriptedEngine()
        let api = FakeAPI()
        let outbox = Outbox(store: store, clock: clock)
        let positions = ListeningPositionStore(store: store, clock: clock)
        let readState = ReadStateCoordinator(api: api, outbox: outbox)
        let controller = PlaybackController(
            engine: engine, positions: positions, readState: readState, clock: clock,
            reportPosition: { id, ms in
                try await api.setPlaybackPosition(episodeId: id, listenedThroughMs: ms).listenedThroughMs
            }
        )
        await controller.activate()
        return Harness(engine: engine, controller: controller, api: api, positions: positions, clock: clock)
    }

    private func episode(serverAt ms: Int) -> EpisodeResponse {
        var episode = Fixture.episode()
        episode.listenedThroughMs = ms
        return episode
    }

    private func load(_ harness: Harness, _ episode: EpisodeResponse, autoplay: Bool = true) async throws {
        try await harness.controller.load(
            episode: episode,
            source: PlaybackController.Source(url: URL(string: "file:///tmp/ep.mp3")!, isLocal: true),
            autoplay: autoplay
        )
    }

    private func reports(_ harness: Harness) async -> [String] {
        await harness.api.successfulCalls().filter { $0.name == "setPlaybackPosition" }.map(\.detail)
    }

    // MARK: - Reading it

    func testAPhoneThatNeverPlayedAnEpisodeResumesWhereTheLaptopGotTo() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 95_000))

        let loaded = await harness.engine.loaded
        XCTAssertEqual(loaded?.startingAtMs, 95_000)
    }

    func testListeningElsewherePastEverythingHeardHereWins() async throws {
        let harness = await makeHarness()
        try await harness.positions.record(episodeId: "ep-1", spokenThroughMs: 40_000, durationMs: 300_000)

        try await load(harness, episode(serverAt: 120_000))

        let loaded = await harness.engine.loaded
        XCTAssertEqual(loaded?.startingAtMs, 120_000)
    }

    func testAScrubBackOnThisPhoneIsNotUndoneByTheServer() async throws {
        // This phone heard to 150s, then the listener scrubbed back to 30s. The server holds
        // 150s — this phone's own report — and must not drag the playhead forward again.
        let harness = await makeHarness()
        try await harness.positions.record(episodeId: "ep-1", spokenThroughMs: 150_000, durationMs: 300_000)
        try await harness.positions.record(episodeId: "ep-1", spokenThroughMs: 30_000, durationMs: 300_000)

        try await load(harness, episode(serverAt: 150_000))

        let loaded = await harness.engine.loaded
        XCTAssertEqual(loaded?.startingAtMs, 30_000)
    }

    func testAFinishedPositionOnTheServerStartsFromTheTop() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 300_000))
        let loaded = await harness.engine.loaded
        XCTAssertEqual(loaded?.startingAtMs, 0)
    }

    // MARK: - Writing it

    func testListeningReportsTheFrontierEveryTenSeconds() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))

        await harness.engine.listen(toMs: 9_000)
        let early = await reports(harness)
        XCTAssertTrue(early.isEmpty, "less than ten seconds heard is not worth a request")

        await harness.engine.listen(toMs: 10_000)
        await harness.engine.listen(toMs: 21_000)
        let sent = await reports(harness)
        XCTAssertEqual(sent, ["ep-1:10000", "ep-1:20000"])
    }

    func testPausingReportsWhateverThereIs() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))
        await harness.engine.listen(toMs: 4_000)

        await harness.controller.perform(.pause)

        let sent = await reports(harness)
        XCTAssertEqual(sent, ["ep-1:4000"])
        let server = await harness.api.serverPosition("ep-1")
        XCTAssertEqual(server, 4_000)
    }

    func testASkippedStoryIsNeverClaimedByAReport() async throws {
        // Heard Alpha's first minute, jumped to Charlie, listened on. The server marks every
        // story a position has passed, so reporting 200s would mark Bravo read unheard.
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))
        await harness.engine.listen(toMs: 30_000)
        await harness.controller.perform(.seek(toMs: 180_000))
        await harness.engine.listen(toMs: 220_000)

        await harness.controller.perform(.pause)

        let server = await harness.api.serverPosition("ep-1")
        XCTAssertEqual(server, 30_000)
    }

    func testResumingAtTheServersPositionReportsOnFromIt() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 95_000))

        await harness.engine.listen(toMs: 110_000)

        let server = await harness.api.serverPosition("ep-1")
        XCTAssertEqual(server, 105_000)
    }

    func testNoSignalIsNotARequestPerTick() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))
        await harness.api.setFailure(.offline)

        await harness.engine.listen(toMs: 15_000)
        let attempts = await harness.api.recordedCalls().filter { $0.name == "setPlaybackPosition" }.count
        XCTAssertEqual(attempts, 1, "one attempt, then quiet until the retry window passes")

        await harness.api.setFailure(nil)
        harness.clock.advance(by: 11)
        await harness.engine.listen(toMs: 16_000)
        let server = await harness.api.serverPosition("ep-1")
        XCTAssertEqual(server, 16_000, "the next report carries the frontier the lost one had")
    }

    func testFinishingReportsTheEnd() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))
        await harness.engine.listen(toMs: 299_000)

        await harness.engine.finish()

        let server = await harness.api.serverPosition("ep-1")
        XCTAssertEqual(server, 300_000)
    }
}

final class MarkListenedTests: XCTestCase {
    func testMarkListenedWritesBothFactsAndFinishesThePositionHere() async throws {
        let store = InMemoryKeyValueStore()
        let clock = TestClock()
        let api = FakeAPI()
        let outbox = Outbox(store: store, clock: clock)
        let positions = ListeningPositionStore(store: store, clock: clock)
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let library = MotetLibrary(
            api: api, cache: store,
            offline: try OfflineLibrary(store: store, directory: directory, downloader: FakeDownloader()),
            positions: positions,
            readState: ReadStateCoordinator(api: api, outbox: outbox),
            clock: clock
        )

        try await library.markListened(episode: Fixture.episode())

        let calls = await api.successfulCalls().map(\.name)
        XCTAssertTrue(calls.contains("markEpisodeListened"))
        XCTAssertEqual(calls.last, "setPlaybackPosition")
        let server = await api.serverPosition("ep-1")
        XCTAssertEqual(server, 300_000)
        let position = try await positions.position(for: "ep-1")
        XCTAssertEqual(position?.isFinished, true)
    }
}
