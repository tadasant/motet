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

    /// Reports go out on a task of their own, so a test waits for what it expects.
    private func waitForServer(_ harness: Harness, _ expected: Int, file: StaticString = #filePath, line: UInt = #line) async throws {
        for _ in 0..<2_000 {
            if await harness.api.serverPosition("ep-1") == expected { return }
            try await Task.sleep(for: .milliseconds(1))
        }
        let got = await harness.api.serverPosition("ep-1")
        XCTFail("server position is \(String(describing: got)), expected \(expected)", file: file, line: line)
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
        try await waitForServer(harness, 10_000)
        await harness.engine.listen(toMs: 21_000)
        try await waitForServer(harness, 20_000)
        let sent = await reports(harness)
        XCTAssertEqual(sent, ["ep-1:10000", "ep-1:20000"])
    }

    func testPausingReportsWhateverThereIs() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))
        await harness.engine.listen(toMs: 4_000)

        await harness.controller.perform(.pause)

        try await waitForServer(harness, 4_000)
        let sent = await reports(harness)
        XCTAssertEqual(sent, ["ep-1:4000"])
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

        try await waitForServer(harness, 30_000)
        try await Task.sleep(for: .milliseconds(20))
        let server = await harness.api.serverPosition("ep-1")
        XCTAssertEqual(server, 30_000, "nothing past the skip is ever claimed")
    }

    func testResumingAtTheServersPositionReportsOnFromIt() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 95_000))

        await harness.engine.listen(toMs: 110_000)

        try await waitForServer(harness, 105_000)
    }

    func testNoSignalIsNotARequestPerTick() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))
        await harness.api.setFailure(.offline)

        await harness.engine.listen(toMs: 10_000)
        for _ in 0..<2_000 {
            if await !harness.api.recordedCalls().isEmpty { break }
            try await Task.sleep(for: .milliseconds(1))
        }
        try await Task.sleep(for: .milliseconds(20))
        await harness.engine.listen(toMs: 15_000)
        try await Task.sleep(for: .milliseconds(20))
        let attempts = await harness.api.recordedCalls().filter { $0.name == "setPlaybackPosition" }.count
        XCTAssertEqual(attempts, 1, "one attempt, then quiet until the retry window passes")

        await harness.api.setFailure(nil)
        harness.clock.advance(by: 11)
        await harness.engine.listen(toMs: 16_000)
        try await waitForServer(harness, 16_000)
    }

    func testFinishingReportsTheEnd() async throws {
        let harness = await makeHarness()
        try await load(harness, episode(serverAt: 0))
        await harness.engine.listen(toMs: 299_000)

        await harness.engine.finish()

        try await waitForServer(harness, 300_000)
    }

    func testAReportStillOutWhenAnotherEpisodeLoadsNeverMovesTheNewOne() async throws {
        // The old episode's answer arrives after the new one is loaded. Taken as the new
        // episode's frontier, it would point the frontier at a place in a different episode.
        let gate = ReportGate()
        let clock = TestClock()
        let store = InMemoryKeyValueStore()
        let api = FakeAPI()
        let engine = ScriptedEngine()
        let controller = PlaybackController(
            engine: engine,
            positions: ListeningPositionStore(store: store, clock: clock),
            readState: ReadStateCoordinator(api: api, outbox: Outbox(store: store, clock: clock)),
            clock: clock,
            reportPosition: { id, ms in try await gate.report(id, ms) }
        )
        await controller.activate()
        let source = PlaybackController.Source(url: URL(string: "file:///tmp/ep.mp3")!, isLocal: true)
        try await controller.load(episode: episode(serverAt: 0), source: source, autoplay: true)
        await engine.listen(toMs: 200_000)
        for _ in 0..<2_000 {
            if await gate.pending > 0 { break }
            try await Task.sleep(for: .milliseconds(1))
        }

        var second = Fixture.episode(id: "ep-2")
        second.listenedThroughMs = 0
        try await controller.load(episode: second, source: source, autoplay: true)
        // The old episode's report answers now, after the new one is loaded.
        await gate.release(answer: 200_000)
        await engine.listen(toMs: 12_000)
        for _ in 0..<2_000 {
            if await gate.sent.contains(where: { $0.hasPrefix("ep-2:") }) { break }
            try await Task.sleep(for: .milliseconds(1))
            await gate.release(answer: nil)
        }

        let sent = await gate.sent
        XCTAssertTrue(sent.contains { $0.hasPrefix("ep-2:") }, "the new episode reports from its own frontier: \(sent)")
        XCTAssertFalse(sent.contains("ep-2:200000"))
    }

    func testASubSecondHoleIsBridgedAndASkipIsNot() {
        let coverage = ListenedCoverage(ranges: [0..<30_000, 30_400..<60_000, 90_000..<120_000])
        XCTAssertEqual(coverage.frontier(from: 0), 60_000)
        XCTAssertEqual(coverage.frontier(from: 60_000), 60_000)
        XCTAssertEqual(coverage.frontier(from: 95_000), 120_000)
    }
}

/// A reporter whose answers the test releases by hand, to hold a report open across a load.
actor ReportGate {
    private(set) var sent: [String] = []
    private var waiting: [CheckedContinuation<Int, Error>] = []
    var pending: Int { waiting.count }

    func report(_ id: String, _ ms: Int) async throws -> Int {
        sent.append("\(id):\(ms)")
        return try await withCheckedThrowingContinuation { waiting.append($0) }
    }

    /// Answer every report still out: with `answer`, or as a failure when nil.
    func release(answer: Int?) {
        let out = waiting
        waiting = []
        for continuation in out {
            if let answer { continuation.resume(returning: answer) } else { continuation.resume(throwing: MotetError.offline) }
        }
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

final class AudioProblemTests: XCTestCase {
    private func client(_ transport: StubTransport) -> MotetHTTPClient {
        MotetHTTPClient(
            configuration: MotetConfiguration(baseURL: URL(string: "https://api.example.invalid")!, apiToken: "s"),
            transport: transport
        )
    }

    func testAudioThatIsGoneSaysSoInTheAPIsWords() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(#"{"detail":"This episode's audio is no longer in storage."}"#, status: 410)

        let problem = await client(transport).audioProblem(episodeId: "ep-1", feedToken: "feed")

        XCTAssertEqual(problem, .gone(reason: "This episode's audio is no longer in storage."))
        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.headers["Range"], "bytes=0-1", "two bytes, never the episode")
        XCTAssertNil(request.headers["Authorization"], "the audio route takes the feed token, not the session")
        XCTAssertTrue(request.url.absoluteString.contains("token=feed"))
    }

    func testAnOlderAPIsRedirectIntoA404IsStillGone() async {
        let transport = StubTransport()
        transport.enqueue(.init(status: 404, body: Data("<Error><Code>NoSuchKey</Code></Error>".utf8)))
        let problem = await client(transport).audioProblem(episodeId: "ep-1", feedToken: "feed")
        XCTAssertEqual(problem, .gone(reason: nil))
    }

    func testAServedFileMeansThePlayerCouldNotPlayIt() async {
        let transport = StubTransport()
        transport.enqueue(.init(status: 206, body: Data([0xFF, 0xFB])))
        let problem = await client(transport).audioProblem(episodeId: "ep-1", feedToken: "feed")
        XCTAssertEqual(problem, .unplayable)
    }

    func testARotatedFeedTokenIsReplacedAndAskedAgain() async throws {
        let store = InMemoryKeyValueStore()
        let clock = TestClock()
        let api = ProbeAPI(answers: [.feedTokenRefused, .unplayable])
        let library = MotetLibrary(
            api: api, cache: store,
            offline: try OfflineLibrary(
                store: store,
                directory: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString),
                downloader: FakeDownloader()
            ),
            positions: ListeningPositionStore(store: store, clock: clock),
            readState: ReadStateCoordinator(api: api, outbox: Outbox(store: store, clock: clock)),
            clock: clock
        )

        let problem = await library.audioProblem(episodeId: "ep-1")

        XCTAssertEqual(problem, .unplayable)
        let feeds = await api.successfulCalls().filter { $0.name == "feedInfo" }.count
        XCTAssertEqual(feeds, 2, "the refused token is forgotten and the current one fetched")
    }
}

/// A FakeAPI whose audio route answers from a script.
actor ProbeAPI: MotetAPI {
    private let fake = FakeAPI()
    private var answers: [AudioProblem]

    init(answers: [AudioProblem]) { self.answers = answers }

    func successfulCalls() async -> [FakeAPI.Call] { await fake.successfulCalls() }
    func audioProblem(episodeId: String, feedToken: String) async -> AudioProblem? {
        answers.isEmpty ? nil : answers.removeFirst()
    }

    func listEpisodes() async throws -> [EpisodeResponse] { try await fake.listEpisodes() }
    func episode(id: String) async throws -> EpisodeResponse { try await fake.episode(id: id) }
    func createEpisode(
        title: String, maxDurationMs: Int, newsItemIds: [String]?, keepInBacklog: Bool
    ) async throws -> EpisodeResponse {
        try await fake.createEpisode(
            title: title, maxDurationMs: maxDurationMs, newsItemIds: newsItemIds, keepInBacklog: keepInBacklog
        )
    }
    func markEpisodeListened(id: String) async throws -> MarkListenedResponse { try await fake.markEpisodeListened(id: id) }
    func setPlaybackPosition(episodeId: String, listenedThroughMs: Int) async throws -> ListenProgressResponse {
        try await fake.setPlaybackPosition(episodeId: episodeId, listenedThroughMs: listenedThroughMs)
    }
    func listNewsItems() async throws -> [NewsItemResponse] { try await fake.listNewsItems() }
    func setNewsItemRead(id: String, read: Bool) async throws -> NewsItemResponse {
        try await fake.setNewsItemRead(id: id, read: read)
    }
    func pasteSource(title: String, text: String) async throws -> SourceItemResponse {
        try await fake.pasteSource(title: title, text: text)
    }
    func feedInfo() async throws -> FeedInfoResponse { try await fake.feedInfo() }
    nonisolated func audioURL(episodeId: String, feedToken: String) throws -> URL {
        URL(string: "https://api.example.invalid/v1/episodes/\(episodeId)/audio?token=\(feedToken)")!
    }
}
