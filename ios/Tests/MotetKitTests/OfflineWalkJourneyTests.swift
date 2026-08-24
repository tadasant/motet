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
    /// A walk, as it actually goes: two stories heard, one skipped past and then gone back
    /// for. The skip is the interesting part — read state follows what was *played*, not
    /// where the playhead has been, so skipping over "Charlie" must not mark it read.
    static let expectedTrace = """
    online  | episodes: ep-3, ep-2, ep-1
    online  | backlog unread: news-a, news-b, news-c
    online  | downloaded for the walk: ep-2, ep-3
    offline | signal lost
    offline | playing ep-3 from the device
    offline | played to 01:30, heard "Alpha" -> read queued
    offline | interrupted by a call at 01:30
    offline | resumed at 01:30, still ours
    offline | played to 03:00, heard "Bravo" -> read queued
    offline | skip forward 30s -> 03:30, nothing heard by skipping
    offline | played to 05:00, "Charlie" still unread: 00:30 of it was never played
    offline | episode ended -> NOT marked listened, because a story was skipped
    offline | queued writes: 2
    offline | went back for the skipped 00:30, heard "Charlie" -> read queued
    online  | signal back, flushed 3 writes
    server  | news-a read, news-b read, news-c read
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

        await engine.listen(toMs: 90_000, stepMs: 5_000)
        trace.append("offline | played to \(stamp(90_000)), heard \"Alpha\" -> read queued")

        // A phone call. The position is ours, so it survives (invariant 4).
        await engine.interrupt(resumable: true)
        let interrupted = await controller.snapshot()
        trace.append("offline | interrupted by a call at \(stamp(interrupted.positionMs))")
        await controller.resumeAfterInterruptionIfNeeded()
        let resumed = await controller.snapshot()
        XCTAssertTrue(resumed.isPlaying)
        trace.append("offline | resumed at \(stamp(resumed.positionMs)), still ours")

        await engine.listen(toMs: 180_000, stepMs: 5_000)
        trace.append("offline | played to \(stamp(180_000)), heard \"Bravo\" -> read queued")

        // The skip. "Charlie" runs 03:00–05:00, so jumping to 03:30 leaves 30 seconds of it
        // that no audio ever played — and read state follows the audio, not the playhead.
        await controller.perform(.skipForward)
        let skipped = await controller.snapshot()
        trace.append(
            "offline | skip forward 30s -> \(stamp(skipped.positionMs)), nothing heard by skipping"
        )

        await engine.listen(toMs: 300_000, stepMs: 5_000)
        trace.append(
            "offline | played to \(stamp(300_000)), \"Charlie\" still unread: "
                + "\(stamp(30_000)) of it was never played"
        )

        await engine.finish()
        // `POST /listened` marks *every* item in the episode read, so it is only honest when
        // every item really was heard. One was skipped, so it does not fire.
        trace.append("offline | episode ended -> NOT marked listened, because a story was skipped")

        let atEnd = try await positions.position(for: "ep-3")
        XCTAssertEqual(atEnd?.isFinished, true, "the file did run out")
        XCTAssertEqual(atEnd?.spokenThroughMs, 300_000)

        let queued = try await outbox.pending()
        trace.append("offline | queued writes: \(queued.count)")
        let duringWalk = await api.successfulCalls().filter {
            $0.name == "setNewsItemRead" || $0.name == "markEpisodeListened"
        }
        XCTAssertTrue(duringWalk.isEmpty, "no write reached the server during the walk")

        // Still on the walk: go back for the 30 seconds the skip jumped over. That is all
        // it takes for "Charlie" to count as heard, and it is why the skip rule is safe —
        // it withholds read state rather than losing it.
        await controller.perform(.seek(toMs: 180_000))
        await controller.perform(.play)
        await engine.listen(toMs: 210_000, stepMs: 5_000)
        trace.append(
            "offline | went back for the skipped \(stamp(30_000)), heard \"Charlie\" -> read queued"
        )

        // --- Home again -------------------------------------------------------------
        await api.setFailure(nil)
        clock.advance(by: 600)
        let outcome = try await library.flushPendingWrites()
        guard case .drained(let count) = outcome else {
            return XCTFail("expected the queue to drain, got \(outcome)")
        }
        trace.append("online  | signal back, flushed \(count) writes")

        trace.append("server  | " + describe(await api.successfulCalls()))

        XCTAssertEqual(trace.joined(separator: "\n"), Self.expectedTrace)

        // Every story ended up read exactly once, and the queue is empty.
        let reads = await api.successfulCalls().filter { $0.name == "setNewsItemRead" }
        XCTAssertEqual(reads.map(\.detail), ["news-a:true", "news-b:true", "news-c:true"])
        let drained = try await outbox.pending()
        XCTAssertTrue(drained.isEmpty)
    }

    /// Reads are not interesting here; the walk is about the writes it owed.
    private func describe(_ calls: [FakeAPI.Call]) -> String {
        calls.compactMap { call in
            switch call.name {
            case "setNewsItemRead": return call.detail.replacingOccurrences(of: ":true", with: " read")
            case "markEpisodeListened": return "\(call.detail) listened"
            default: return nil
            }
        }.joined(separator: ", ")
    }

    private func stamp(_ ms: Int) -> String {
        String(format: "%02d:%02d", ms / 60_000, (ms % 60_000) / 1_000)
    }
}
