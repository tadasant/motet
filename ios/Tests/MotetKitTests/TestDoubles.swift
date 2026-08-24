import Foundation
import XCTest
@testable import MotetKit

// MARK: - API

/// An in-memory Motet API, with a switch for "the network is gone".
///
/// Deliberately not a mock of HTTP: the tests that care about the wire use `StubTransport`
/// against the real `MotetHTTPClient`, and the tests that care about behaviour use this.
actor FakeAPI: MotetAPI {
    struct Call: Hashable {
        let name: String
        let detail: String
    }

    /// Every attempt, including ones that threw — the outbox tests assert on attempts.
    private(set) var calls: [Call] = []
    /// Only the attempts that went through.
    private(set) var succeeded: [Call] = []
    var episodes: [EpisodeResponse] = []
    var newsItems: [NewsItemResponse] = []
    var feed = FeedInfoResponse(token: "feed-token", url: "https://example.invalid/feed.xml?token=feed-token")
    /// When set, every call throws it. Simulates a walk with no signal.
    var failure: MotetError?
    var baseURL = URL(string: "https://api.example.invalid")!

    func setFailure(_ error: MotetError?) { failure = error }
    func setEpisodes(_ episodes: [EpisodeResponse]) { self.episodes = episodes }
    func setNewsItems(_ items: [NewsItemResponse]) { self.newsItems = items }
    func recordedCalls() -> [Call] { calls }
    func successfulCalls() -> [Call] { succeeded }

    private func check(_ name: String, _ detail: String = "") throws {
        let call = Call(name: name, detail: detail)
        calls.append(call)
        if let failure { throw failure }
        succeeded.append(call)
    }

    func listEpisodes() async throws -> [EpisodeResponse] {
        try check("listEpisodes")
        return episodes
    }

    func episode(id: String) async throws -> EpisodeResponse {
        try check("episode", id)
        guard let match = episodes.first(where: { $0.id == id }) else {
            throw MotetError.http(status: 404, detail: nil)
        }
        return match
    }

    func createEpisode(title: String, maxDurationMs: Int) async throws -> EpisodeResponse {
        try check("createEpisode", title)
        return EpisodeResponse(
            audioBytes: nil, audioMediaType: nil, createdAt: Date(), durationMs: 0,
            id: "new", lastError: nil, maxDurationMs: maxDurationMs, publishedAt: nil,
            segments: [], state: "pending", title: title
        )
    }

    func markEpisodeListened(id: String) async throws -> MarkListenedResponse {
        try check("markEpisodeListened", id)
        return MarkListenedResponse(episodeId: id, newsItemsMarkedRead: 0)
    }

    func listNewsItems() async throws -> [NewsItemResponse] {
        try check("listNewsItems")
        return newsItems
    }

    func setNewsItemRead(id: String, read: Bool) async throws -> NewsItemResponse {
        try check("setNewsItemRead", "\(id):\(read)")
        if let index = newsItems.firstIndex(where: { $0.id == id }) {
            newsItems[index].read = read
            return newsItems[index]
        }
        return NewsItemResponse(
            createdAt: Date(), id: id, read: read, sourceItemIds: [], summary: "", title: id
        )
    }

    func pasteSource(title: String, text: String) async throws -> SourceItemResponse {
        try check("pasteSource", title)
        return SourceItemResponse(id: "source", state: "pending", title: title)
    }

    func feedInfo() async throws -> FeedInfoResponse {
        try check("feedInfo")
        return feed
    }

    nonisolated func audioURL(episodeId: String, feedToken: String) throws -> URL {
        URL(string: "https://api.example.invalid/v1/episodes/\(episodeId)/audio?token=\(feedToken)")!
    }
}

// MARK: - Transport

/// A transport that answers from a script and records what it was asked.
final class StubTransport: HTTPTransport, @unchecked Sendable {
    struct Exchange {
        var status: Int = 200
        var body: Data = Data("{}".utf8)
        var error: Error?
    }

    private let lock = NSLock()
    private var queued: [Exchange] = []
    private(set) var requests: [HTTPRequest] = []

    init(_ exchanges: [Exchange] = []) {
        queued = exchanges
    }

    func enqueue(_ exchange: Exchange) {
        lock.withLock { queued.append(exchange) }
    }

    func enqueueJSON(_ json: String, status: Int = 200) {
        enqueue(Exchange(status: status, body: Data(json.utf8)))
    }

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        let exchange: Exchange? = lock.withLock {
            requests.append(request)
            return queued.isEmpty ? nil : queued.removeFirst()
        }
        guard let exchange else {
            throw MotetError.transport(URLError(.unsupportedURL))
        }
        if let error = exchange.error { throw error }
        return HTTPResponse(
            statusCode: exchange.status,
            headers: ["content-type": "application/json"],
            body: exchange.body
        )
    }

    func recordedRequests() -> [HTTPRequest] { lock.withLock { requests } }
}

// MARK: - Playback engine

/// A playback engine the test drives by hand: no audio, no timers, no waiting.
actor ScriptedEngine: PlaybackEngine {
    struct LoadedItem: Equatable {
        let url: URL
        let startingAtMs: Int
    }

    private(set) var loaded: LoadedItem?
    private(set) var isPlaying = false
    private(set) var rate: Double = 1.0
    private(set) var seeks: [Int] = []
    private var handler: (@Sendable (PlaybackEngineEvent) async -> Void)?
    private var positionMs = 0
    var loadError: Error?

    func setLoadError(_ error: Error?) { loadError = error }

    func load(url: URL, startingAtMs: Int) async throws {
        if let loadError { throw loadError }
        loaded = LoadedItem(url: url, startingAtMs: startingAtMs)
        positionMs = startingAtMs
        await emit(.ready(durationMs: 0))
    }

    func play() async {
        isPlaying = true
        await emit(.playing)
    }

    func pause() async {
        isPlaying = false
        await emit(.paused)
    }

    func seek(toMs ms: Int) async {
        seeks.append(ms)
        positionMs = ms
        // `AVPlayerPlaybackEngine.seek` emits this, so the fake does too — a double that
        // is quieter than the real thing hides bugs in exactly the code it is testing.
        await emit(.position(ms: ms))
    }

    func setRate(_ rate: Double) async {
        self.rate = rate
    }

    func currentTimeMs() async -> Int { positionMs }

    func setEventHandler(_ handler: @escaping @Sendable (PlaybackEngineEvent) async -> Void) async {
        self.handler = handler
    }

    // Test-side drivers.

    /// Play through to `ms` the way the real engine reports it: about once a second.
    ///
    /// Distinct from `advance(toMs:)`, which is a *jump* — the difference is the whole
    /// point of `ListenedCoverage`, so the doubles keep it visible.
    func listen(toMs ms: Int, stepMs: Int = 1_000) async {
        var next = positionMs
        while next < ms {
            next = min(ms, next + stepMs)
            await advance(toMs: next)
        }
    }

    func advance(toMs ms: Int) async {
        positionMs = ms
        await emit(.position(ms: ms))
    }

    func finish() async {
        await emit(.ended)
    }

    func interrupt(resumable: Bool) async {
        isPlaying = false
        await emit(.interrupted(resumable: resumable))
    }

    func fail(_ message: String) async {
        await emit(.failed(message))
    }

    private func emit(_ event: PlaybackEngineEvent) async {
        await handler?(event)
    }
}

// MARK: - Downloader

/// A downloader that writes a known payload, and can be told to fail.
final class FakeDownloader: EpisodeDownloader, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var downloaded: [URL] = []
    var payload = Data("audio-bytes".utf8)
    var failingURLs: Set<URL> = []

    func download(from url: URL, to destination: URL) async throws {
        let shouldFail: Bool = lock.withLock {
            downloaded.append(url)
            return failingURLs.contains(url)
        }
        if shouldFail { throw MotetError.offline }
        try lock.withLock { payload }.write(to: destination, options: .atomic)
    }

    func recordedDownloads() -> [URL] { lock.withLock { downloaded } }
}

// MARK: - Fixtures

enum Fixture {
    /// An episode with three stories: two segments for the first, one each for the rest.
    static func episode(
        id: String = "ep-1",
        state: String = "ready",
        durationMs: Int = 300_000,
        createdAt: Date = Date(timeIntervalSince1970: 1_800_000_000)
    ) -> EpisodeResponse {
        EpisodeResponse(
            audioBytes: 1_024,
            audioMediaType: "audio/mpeg",
            createdAt: createdAt,
            durationMs: durationMs,
            id: id,
            lastError: nil,
            maxDurationMs: 600_000,
            publishedAt: createdAt,
            segments: [
                segment(newsItemId: "news-a", title: "Alpha", startMs: 0, durationMs: 60_000),
                segment(newsItemId: "news-a", title: "Alpha", startMs: 60_000, durationMs: 30_000),
                segment(newsItemId: "news-b", title: "Bravo", startMs: 90_000, durationMs: 90_000),
                segment(newsItemId: "news-c", title: "Charlie", startMs: 180_000, durationMs: 120_000),
            ],
            state: state,
            title: "Morning briefing"
        )
    }

    static func segment(
        newsItemId: String, title: String, startMs: Int, durationMs: Int
    ) -> SegmentResponse {
        SegmentResponse(
            claims: [
                ClaimModel(
                    sourceExcerpt: "raised $12m",
                    sourceTitle: "Newsletter",
                    span: SourceSpanModel(end: 40, sourceItemId: "src-1", start: 20),
                    text: "They raised twelve million dollars."
                )
            ],
            durationMs: durationMs,
            newsItemId: newsItemId,
            newsItemTitle: title,
            startMs: startMs,
            text: "…"
        )
    }

    static func newsItem(id: String, read: Bool = false) -> NewsItemResponse {
        NewsItemResponse(
            createdAt: Date(timeIntervalSince1970: 1_800_000_000),
            id: id,
            read: read,
            sourceItemIds: ["src-1"],
            summary: "A story.",
            title: id.capitalized
        )
    }

    /// A temporary directory the test owns, removed when it is done.
    static func temporaryDirectory(_ testCase: XCTestCase) -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("motet-test-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        testCase.addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }
}
