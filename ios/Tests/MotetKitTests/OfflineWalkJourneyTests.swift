import XCTest
@testable import MotetKit

/// The end-to-end journey this app exists for: download at home, listen with no signal,
/// and have the backlog agree with the web when the phone finds a network again.
///
/// It drives the real `MotetLibrary`, `OfflineLibrary`, `PlaybackController`, `Outbox` and
/// `ReadStateCoordinator` together — only the audio engine, the downloader, and the network
/// are doubles. The assertion is a **trace**: the whole walk as a readable transcript, so a
/// change in behaviour shows up as a diff of what happened rather than as a failed boolean.
final class OfflineWalkJourneyTests: XCTestCase {
    /// What a walk looks like when everything works.
    static let expectedTrace = """
    online  | episodes: ep-3, ep-2, ep-1
    online  | backlog unread: news-a, news-b, news-c
    online  | downloaded for the walk: ep-2, ep-3
    offline | signal lost
    offline | playing ep-3 from the device
    offline | 01:30 heard "Alpha" -> read state queued
    offline | interrupted by a call at 01:30
    offline | resumed at 01:30, still ours
    offline | 03:00 heard "Bravo" -> read state queued
    offline | skip forward 30s -> 03:30
    offline | 05:00 heard "Charlie" -> read state queued
    offline | episode finished -> listened queued
    offline | queued writes: 4
    online  | signal back, flushed 4 writes
    server  | news-a read, news-b read, news-c read, ep-3 listened
    """

    func testADogWalkWithNoSignal() async throws {
        var trace: [String] = []
        let clock = TestClock()
        let store = InMemoryKeyValueStore()
        let api = FakeAPI()
        let engine = ScriptedEngine()

        let offline = try OfflineLibrary(
            store: store,
            directory: Fixture.temporaryDirectory(self),
            downloader: FakeDownloader(),
            clock: clock
        )
        let outbox = Outbox(store: store, clock: clock)
        let readState = ReadStateCoordinator(api: api, outbox: outbox)
        let positions = ListeningPositionStore(store: store, clock: clock)
        let library = MotetLibrary(
            api: api, cache: store, offline: offline,
            positions: positions, readState: readState, clock: clock
        )
        let controller = PlaybackController(
            engine: engine, positions: positions, readState: readState,
            settings: PlaybackSettings(episodesToKeepOffline: 2), clock: clock
        )
        await controller.activate()
        try await library.update(settings: PlaybackSettings(episodesToKeepOffline: 2))

        // --- At home, on wifi -------------------------------------------------------
        // Newest first, as `GET /v1/episodes` documents.
        let episodes = (1...3).reversed().map {
            Fixture.episode(
                id: "ep-\($0)",
                createdAt: Date(timeIntervalSince1970: 1_800_000_000 + Double($0) * 86_400)
            )
        }
        await api.setEpisodes(episodes)
        await api.setNewsItems(["news-a", "news-b", "news-c"].map { Fixture.newsItem(id: $0) })

        let listed = try await library.episodes()
        trace.append("online  | episodes: " + listed.map(\.id).joined(separator: ", "))

        let backlog = try await library.newsItems()
        trace.append(
            "online  | backlog unread: "
                + backlog.filter { !$0.read }.map(\.id).joined(separator: ", ")
        )

        _ = try await library.syncDownloads(episodes: listed)
        let held = try await library.downloadedEpisodeIds()
        trace.append("online  | downloaded for the walk: " + held.sorted().joined(separator: ", "))

        // --- Out of the door --------------------------------------------------------
        await api.setFailure(.offline)
        trace.append("offline | signal lost")

        let episode = try XCTUnwrap(listed.first)
        let source = try await library.source(forEpisode: episode)
        XCTAssertTrue(source.isLocal, "no network was used to start playback")
        try await controller.load(episode: episode, source: source, autoplay: true)
        trace.append("offline | playing \(episode.id) from the device")

        await engine.advance(toMs: 90_000)
        trace.append("offline | \(stamp(90_000)) heard \"Alpha\" -> read state queued")

        // A phone call. The position is ours, so it survives (invariant 4).
        await engine.interrupt(resumable: true)
        let interrupted = await controller.snapshot()
        trace.append("offline | interrupted by a call at \(stamp(interrupted.positionMs))")
        await controller.resumeAfterInterruptionIfNeeded()
        let resumed = await controller.snapshot()
        XCTAssertTrue(resumed.isPlaying)
        trace.append("offline | resumed at \(stamp(resumed.positionMs)), still ours")

        await engine.advance(toMs: 180_000)
        trace.append("offline | \(stamp(180_000)) heard \"Bravo\" -> read state queued")

        await controller.perform(.skipForward)
        let skipped = await controller.snapshot()
        trace.append("offline | skip forward 30s -> \(stamp(skipped.positionMs))")

        await engine.advance(toMs: 300_000)
        trace.append("offline | \(stamp(300_000)) heard \"Charlie\" -> read state queued")

        await engine.finish()
        trace.append("offline | episode finished -> listened queued")

        let queued = try await outbox.pending()
        trace.append("offline | queued writes: \(queued.count)")
        let duringWalk = await api.successfulCalls().filter {
            $0.name == "setNewsItemRead" || $0.name == "markEpisodeListened"
        }
        XCTAssertTrue(duringWalk.isEmpty, "no write reached the server during the walk")

        // --- Home again -------------------------------------------------------------
        await api.setFailure(nil)
        clock.advance(by: 600)
        let outcome = try await library.flushPendingWrites()
        guard case .drained(let count) = outcome else {
            return XCTFail("expected the queue to drain, got \(outcome)")
        }
        trace.append("online  | signal back, flushed \(count) writes")

        // Reads are not interesting here; the walk is about the writes it owed.
        let sent = await api.successfulCalls().filter {
            $0.name == "setNewsItemRead" || $0.name == "markEpisodeListened"
        }
        trace.append("server  | " + sent.map { call in
            switch call.name {
            case "setNewsItemRead": return call.detail.replacingOccurrences(of: ":true", with: " read")
            case "markEpisodeListened": return "\(call.detail) listened"
            default: return call.name
            }
        }.joined(separator: ", "))

        XCTAssertEqual(trace.joined(separator: "\n"), Self.expectedTrace)

        // And the position we own says the episode is done.
        let position = try await positions.position(for: "ep-3")
        XCTAssertEqual(position?.isFinished, true)
        XCTAssertEqual(position?.spokenThroughMs, 300_000)
        let drained = try await outbox.pending()
        XCTAssertTrue(drained.isEmpty)
    }

    private func stamp(_ ms: Int) -> String {
        String(format: "%02d:%02d", ms / 60_000, (ms % 60_000) / 1_000)
    }
}
