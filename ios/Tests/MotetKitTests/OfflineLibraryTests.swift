import XCTest
@testable import MotetKit

/// The offline half: the best listening happens where the signal is worst.
final class OfflineLibraryTests: XCTestCase {
    private func makeLibrary(
        store: InMemoryKeyValueStore = InMemoryKeyValueStore(),
        downloader: FakeDownloader = FakeDownloader(),
        directory: URL
    ) throws -> OfflineLibrary {
        try OfflineLibrary(
            store: store, directory: directory, downloader: downloader, clock: TestClock()
        )
    }

    func testTheNewestReadyEpisodesArePlannedForDownload() {
        let episodes = (1...8).map {
            Fixture.episode(id: "ep-\($0)", createdAt: Date(timeIntervalSince1970: 1_800_000_000 + Double($0) * 86_400))
        }
        let plan = DownloadPolicy.plan(episodes: episodes, downloaded: [], keep: 3)
        XCTAssertEqual(plan.toDownload, ["ep-8", "ep-7", "ep-6"])
        XCTAssertTrue(plan.toEvict.isEmpty)
    }

    func testAnEpisodeThatIsNotReadyIsNotDownloaded() {
        let episodes = [
            Fixture.episode(id: "ready"),
            Fixture.episode(id: "rendering", state: "rendering"),
            Fixture.episode(id: "failed", state: "failed"),
        ]
        let plan = DownloadPolicy.plan(episodes: episodes, downloaded: [], keep: 5)
        XCTAssertEqual(plan.toDownload, ["ready"])
    }

    func testOlderEpisodesAreEvictedAndThePlayingOneIsNot() {
        let episodes = (1...4).map {
            Fixture.episode(id: "ep-\($0)", createdAt: Date(timeIntervalSince1970: 1_800_000_000 + Double($0) * 86_400))
        }
        let plan = DownloadPolicy.plan(
            episodes: episodes, downloaded: ["ep-1", "ep-2", "ep-4"], keep: 1, pinned: ["ep-2"]
        )
        XCTAssertEqual(plan.toEvict, ["ep-1"])
        XCTAssertTrue(plan.toDownload.isEmpty)
    }

    func testBytesForAnEpisodeTheServerNoLongerListsAreReclaimed() {
        let plan = DownloadPolicy.plan(
            episodes: [Fixture.episode(id: "ep-1")], downloaded: ["ep-1", "deleted"], keep: 5
        )
        XCTAssertEqual(plan.toEvict, ["deleted"])
    }

    func testDownloadWritesAFileAndRemembersIt() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        let library = try makeLibrary(downloader: downloader, directory: directory)

        let url = URL(string: "https://api.example.invalid/v1/episodes/ep-1/audio?token=t")!
        let local = try await library.download(episodeId: "ep-1", from: url)

        XCTAssertTrue(FileManager.default.fileExists(atPath: local.path))
        XCTAssertEqual(try Data(contentsOf: local), Data("audio-bytes".utf8))
        let ids = try await library.downloadedEpisodeIds()
        XCTAssertEqual(ids, ["ep-1"])
        let bytes = try await library.totalBytes()
        XCTAssertEqual(bytes, 11)
    }

    func testDownloadingTwiceDoesNotFetchTwice() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        let library = try makeLibrary(downloader: downloader, directory: directory)
        let url = URL(string: "https://example.invalid/a")!

        _ = try await library.download(episodeId: "ep-1", from: url)
        _ = try await library.download(episodeId: "ep-1", from: url)

        XCTAssertEqual(downloader.recordedDownloads().count, 1)
    }

    func testTheManifestSurvivesRelaunchAndTrustsTheDisk() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let store = InMemoryKeyValueStore()
        let first = try makeLibrary(store: store, directory: directory)
        _ = try await first.download(
            episodeId: "ep-1", from: URL(string: "https://example.invalid/a")!
        )
        let path = try await first.localURL(forEpisode: "ep-1")

        // iOS evicted the file under storage pressure while the app was not running.
        try FileManager.default.removeItem(at: try XCTUnwrap(path))

        let second = try makeLibrary(store: store, directory: directory)
        let ids = try await second.downloadedEpisodeIds()
        XCTAssertTrue(ids.isEmpty, "a phantom entry would make playback open a missing file")
    }

    func testOneFailedDownloadDoesNotStopTheOthers() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        let bad = URL(string: "https://example.invalid/v1/episodes/ep-2/audio")!
        downloader.failingURLs = [bad]
        let library = try makeLibrary(downloader: downloader, directory: directory)

        let plan = DownloadPolicy.Plan(toDownload: ["ep-1", "ep-2", "ep-3"], toEvict: [])
        let failures = await library.apply(plan) { id in
            URL(string: "https://example.invalid/v1/episodes/\(id)/audio")!
        }

        XCTAssertEqual(Array(failures.keys), ["ep-2"])
        let ids = try await library.downloadedEpisodeIds()
        XCTAssertEqual(ids, ["ep-1", "ep-3"])
    }

    func testRemovingADownloadDeletesTheFile() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let library = try makeLibrary(directory: directory)
        let local = try await library.download(
            episodeId: "ep-1", from: URL(string: "https://example.invalid/a")!
        )
        try await library.remove(episodeId: "ep-1")

        XCTAssertFalse(FileManager.default.fileExists(atPath: local.path))
        let ids = try await library.downloadedEpisodeIds()
        XCTAssertTrue(ids.isEmpty)
    }

    func testAnEpisodeIdWithASlashCannotEscapeTheDownloadDirectory() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let library = try makeLibrary(directory: directory)
        let local = try await library.download(
            episodeId: "../escaped", from: URL(string: "https://example.invalid/a")!
        )
        XCTAssertEqual(
            local.deletingLastPathComponent().standardizedFileURL.path,
            directory.standardizedFileURL.path
        )
    }
}
