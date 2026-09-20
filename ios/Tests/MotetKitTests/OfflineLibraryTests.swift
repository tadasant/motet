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
        XCTAssertEqual(try Data(contentsOf: local), Audio.mp3)
        let ids = try await library.downloadedEpisodeIds()
        XCTAssertEqual(ids, ["ep-1"])
        let bytes = try await library.totalBytes()
        XCTAssertEqual(bytes, Audio.mp3.count)
    }

    // MARK: - The file has to be named what it is

    /// The bug behind `AVFoundationErrorDomain -11828` on a downloaded episode: AVFoundation
    /// picks a reader for a *local* file from its path extension and from nothing else, so
    /// the `.audio` every download used to be named made a perfectly good MP3 unopenable.
    func testADownloadIsNamedAfterTheFormatItsBytesActuallyAre() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        let library = try makeLibrary(downloader: downloader, directory: directory)

        let local = try await library.download(
            episodeId: "ep-1", from: URL(string: "https://example.invalid/a")!
        )

        XCTAssertEqual(local.pathExtension, "mp3")
        XCTAssertNotNil(
            AudioFileFormat(pathExtension: local.pathExtension),
            "a local file whose extension AVFoundation does not know fails to open with -11828"
        )
    }

    func testAWavEpisodeIsNamedWav() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        downloader.payload = Audio.wav
        let library = try makeLibrary(downloader: downloader, directory: directory)

        let local = try await library.download(
            episodeId: "ep-1", from: URL(string: "https://example.invalid/a")!
        )

        XCTAssertEqual(local.pathExtension, "wav")
    }

    /// A 200 carrying a sign-in page is one of the ways -11828 arrives, and filing it would
    /// be worse than failing: `source(forEpisode:)` prefers the device, so a held HTML file
    /// shadows the API for that episode on every future tap.
    func testSomethingThatIsNotAudioIsNotFiledAsAnEpisode() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        downloader.payload = Audio.signInPage
        let library = try makeLibrary(downloader: downloader, directory: directory)

        do {
            _ = try await library.download(
                episodeId: "ep-1", from: URL(string: "https://example.invalid/a")!
            )
            XCTFail("an unplayable download must not be filed")
        } catch is UnplayableAudioError {
            // expected
        }

        let ids = try await library.downloadedEpisodeIds()
        XCTAssertTrue(ids.isEmpty)
        XCTAssertEqual(
            try FileManager.default.contentsOfDirectory(atPath: directory.path), [],
            "the staged file has to go too, or it is a leak and a phantom"
        )
    }

    func testAnEmptyDownloadIsRefused() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let downloader = FakeDownloader()
        downloader.payload = Data()
        let library = try makeLibrary(downloader: downloader, directory: directory)

        do {
            _ = try await library.download(
                episodeId: "ep-1", from: URL(string: "https://example.invalid/a")!
            )
            XCTFail("a zero-byte download must not be filed")
        } catch let error as UnplayableAudioError {
            XCTAssertEqual(error.byteCount, 0)
        }
    }

    /// What happens on Tadas's phone when this build replaces the one that wrote `.audio`:
    /// the bytes are good, so they are renamed rather than re-fetched over cellular.
    func testAnEpisodeDownloadedByAnOlderBuildIsRenamedRatherThanRefetched() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let store = InMemoryKeyValueStore()
        let legacy = directory.appendingPathComponent("ep-1.audio")
        try Audio.mp3.write(to: legacy)
        try store.setValue(
            [DownloadedEpisode(
                episodeId: "ep-1",
                fileName: "ep-1.audio",
                byteCount: Audio.mp3.count,
                downloadedAt: Date(timeIntervalSince1970: 1_800_000_000)
            )],
            forKey: "offline.manifest"
        )

        let downloader = FakeDownloader()
        let library = try makeLibrary(store: store, downloader: downloader, directory: directory)
        let local = try await library.localURL(forEpisode: "ep-1")

        XCTAssertEqual(local?.lastPathComponent, "ep-1.mp3")
        XCTAssertEqual(try Data(contentsOf: try XCTUnwrap(local)), Audio.mp3)
        XCTAssertFalse(FileManager.default.fileExists(atPath: legacy.path))
        XCTAssertEqual(downloader.recordedDownloads(), [], "renaming must not cost a download")

        // And it survives the relaunch after the one that repaired it.
        let next = try makeLibrary(store: store, directory: directory)
        let again = try await next.localURL(forEpisode: "ep-1")
        XCTAssertEqual(again?.lastPathComponent, "ep-1.mp3")
    }

    func testAnOlderBuildsFileThatIsNotAudioIsDroppedSoTheNextSyncRefetchesIt() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let store = InMemoryKeyValueStore()
        try Audio.signInPage.write(to: directory.appendingPathComponent("ep-1.audio"))
        try store.setValue(
            [DownloadedEpisode(
                episodeId: "ep-1",
                fileName: "ep-1.audio",
                byteCount: Audio.signInPage.count,
                downloadedAt: Date(timeIntervalSince1970: 1_800_000_000)
            )],
            forKey: "offline.manifest"
        )

        let library = try makeLibrary(store: store, directory: directory)

        let ids = try await library.downloadedEpisodeIds()
        XCTAssertTrue(ids.isEmpty)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path), [])
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
