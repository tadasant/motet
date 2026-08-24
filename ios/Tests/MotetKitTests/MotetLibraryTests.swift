import XCTest
@testable import MotetKit

/// The facade the app binds to, and the offline-first behaviour it owes the listener.
final class MotetLibraryTests: XCTestCase {
    private struct Harness {
        let library: MotetLibrary
        let api: FakeAPI
        let outbox: Outbox
        let downloader: FakeDownloader
    }

    private func makeHarness(_ testCase: XCTestCase) throws -> Harness {
        let clock = TestClock()
        let store = InMemoryKeyValueStore()
        let api = FakeAPI()
        let downloader = FakeDownloader()
        let offline = try OfflineLibrary(
            store: store,
            directory: Fixture.temporaryDirectory(testCase),
            downloader: downloader,
            clock: clock
        )
        let outbox = Outbox(store: store, clock: clock)
        let library = MotetLibrary(
            api: api,
            cache: store,
            offline: offline,
            positions: ListeningPositionStore(store: store, clock: clock),
            readState: ReadStateCoordinator(api: api, outbox: outbox),
            clock: clock
        )
        return Harness(library: library, api: api, outbox: outbox, downloader: downloader)
    }

    func testTheLibraryOpensFromCacheWithNoSignal() async throws {
        let harness = try makeHarness(self)
        await harness.api.setEpisodes([Fixture.episode()])
        _ = try await harness.library.episodes()

        await harness.api.setFailure(.offline)
        let offlineEpisodes = try await harness.library.episodes()

        XCTAssertEqual(offlineEpisodes.map(\.id), ["ep-1"], "the backlog still opens")
    }

    func testAnEmptyCacheWithNoSignalReportsTheFailureRatherThanPretending() async throws {
        let harness = try makeHarness(self)
        await harness.api.setFailure(.offline)
        do {
            _ = try await harness.library.episodes()
            XCTFail("expected a failure")
        } catch let error as MotetError {
            guard case .offline = error else { return XCTFail("got \(error)") }
        }
    }

    func testAnExpiredTokenIsNotPapredOverWithACachedList() async throws {
        let harness = try makeHarness(self)
        await harness.api.setEpisodes([Fixture.episode()])
        _ = try await harness.library.episodes()

        await harness.api.setFailure(.unauthorized)
        do {
            _ = try await harness.library.episodes()
            XCTFail("expected a failure")
        } catch let error as MotetError {
            guard case .unauthorized = error else { return XCTFail("got \(error)") }
        }
    }

    func testABacklogTapMadeOfflineShowsAsReadImmediately() async throws {
        let harness = try makeHarness(self)
        await harness.api.setNewsItems([Fixture.newsItem(id: "news-a")])
        _ = try await harness.library.newsItems()

        await harness.api.setFailure(.offline)
        try await harness.library.setRead(true, newsItemId: "news-a")
        let items = try await harness.library.newsItems()

        XCTAssertEqual(items.first?.read, true, "a pending write must not flip back under the thumb")
        let pending = try await harness.outbox.pending()
        XCTAssertEqual(pending.count, 1)
    }

    func testPlaybackPrefersTheDownloadedCopy() async throws {
        let harness = try makeHarness(self)
        let episode = Fixture.episode()

        let remote = try await harness.library.source(forEpisode: episode)
        XCTAssertFalse(remote.isLocal)
        XCTAssertEqual(remote.url.absoluteString.contains("token=feed-token"), true)

        try await harness.library.download(episode: episode)
        let local = try await harness.library.source(forEpisode: episode)
        XCTAssertTrue(local.isLocal)
        XCTAssertEqual(local.url.isFileURL, true)
    }

    func testTheFeedTokenIsFetchedOnceAndCachedForOfflineDownloads() async throws {
        let harness = try makeHarness(self)
        await harness.api.setEpisodes([Fixture.episode()])
        _ = try await harness.library.download(episode: Fixture.episode())
        _ = try await harness.library.source(forEpisode: Fixture.episode(id: "ep-2"))

        let feedCalls = await harness.api.recordedCalls().filter { $0.name == "feedInfo" }
        XCTAssertEqual(feedCalls.count, 1)
    }

    func testSyncDownloadsFetchesTheNewestAndDropsTheRest() async throws {
        let harness = try makeHarness(self)
        var settings = try await harness.library.playbackSettings()
        settings = PlaybackSettings(
            rate: settings.rate,
            skipForwardMs: settings.skipForwardMs,
            skipBackwardMs: settings.skipBackwardMs,
            episodesToKeepOffline: 2
        )
        try await harness.library.update(settings: settings)

        let episodes = (1...4).map {
            Fixture.episode(id: "ep-\($0)", createdAt: Date(timeIntervalSince1970: 1_800_000_000 + Double($0) * 86_400))
        }
        let failures = try await harness.library.syncDownloads(episodes: episodes)
        XCTAssertTrue(failures.isEmpty)

        let ids = try await harness.library.downloadedEpisodeIds()
        XCTAssertEqual(ids, ["ep-3", "ep-4"])
    }

    func testSettingsSurviveRelaunch() async throws {
        let harness = try makeHarness(self)
        try await harness.library.update(settings: PlaybackSettings(rate: 1.75))
        let stored = try await harness.library.playbackSettings()
        XCTAssertEqual(stored.rate, 1.75)
    }
}
