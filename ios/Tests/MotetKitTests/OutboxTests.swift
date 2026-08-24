import XCTest
@testable import MotetKit

/// The queue that makes read state survive a walk with no signal.
final class OutboxTests: XCTestCase {
    func testWritesGoOutInOrder() async throws {
        let api = FakeAPI()
        let outbox = Outbox(store: InMemoryKeyValueStore(), clock: TestClock())
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: true))
        try await outbox.enqueue(.newsItemRead(newsItemId: "b", read: true))

        let outcome = try await outbox.drain(using: api)

        XCTAssertEqual(outcome, .drained(count: 2))
        let calls = await api.recordedCalls()
        XCTAssertEqual(calls.map(\.detail), ["a:true", "b:true"])
        let pending = try await outbox.pending()
        XCTAssertTrue(pending.isEmpty)
    }

    func testTheSameFactQueuedTwiceIsSentOnce() async throws {
        let api = FakeAPI()
        let outbox = Outbox(store: InMemoryKeyValueStore(), clock: TestClock())
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: true))
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: false))
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: true))

        _ = try await outbox.drain(using: api)

        let calls = await api.recordedCalls()
        XCTAssertEqual(calls.map(\.detail), ["a:true"], "last write wins, once")
    }

    func testOfflineKeepsTheQueueAndRetriesLater() async throws {
        let clock = TestClock()
        let api = FakeAPI()
        await api.setFailure(.offline)
        let outbox = Outbox(store: InMemoryKeyValueStore(), clock: clock)
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: true))

        let first = try await outbox.drain(using: api)
        XCTAssertEqual(first, .deferred(remaining: 1))

        // Backoff: retrying immediately does not even attempt the call.
        let callsAfterFirst = await api.recordedCalls().count
        _ = try await outbox.drain(using: api)
        let callsAfterSecond = await api.recordedCalls().count
        XCTAssertEqual(callsAfterFirst, callsAfterSecond, "held back by the backoff")

        clock.advance(by: 5)
        await api.setFailure(nil)
        let third = try await outbox.drain(using: api)
        XCTAssertEqual(third, .drained(count: 1))
    }

    func testAPermanentFailureIsDroppedRatherThanWedgingTheQueue() async throws {
        let api = FakeAPI()
        await api.setFailure(.http(status: 422, detail: "no such news item"))
        let outbox = Outbox(store: InMemoryKeyValueStore(), clock: TestClock())
        try await outbox.enqueue(.newsItemRead(newsItemId: "gone", read: true))
        try await outbox.enqueue(.newsItemRead(newsItemId: "fine", read: true))

        _ = try await outbox.drain(using: api)
        await api.setFailure(nil)
        _ = try await outbox.drain(using: api)

        let pending = try await outbox.pending()
        XCTAssertTrue(pending.isEmpty, "the poison entry did not block the good one")
    }

    func testTheQueueSurvivesTheProcessBeingKilled() async throws {
        let store = InMemoryKeyValueStore()
        let first = Outbox(store: store, clock: TestClock())
        try await first.enqueue(.newsItemRead(newsItemId: "a", read: true))
        try await first.enqueue(.episodeListened(episodeId: "ep-1"))

        // A brand new Outbox over the same storage: the app relaunched.
        let second = Outbox(store: store, clock: TestClock())
        let pending = try await second.pending()
        XCTAssertEqual(pending.count, 2)

        let api = FakeAPI()
        _ = try await second.drain(using: api)
        let names = await api.recordedCalls().map(\.name)
        XCTAssertEqual(names, ["setNewsItemRead", "markEpisodeListened"])
    }

    func testPendingReadStateIsVisibleBeforeItDrains() async throws {
        let outbox = Outbox(store: InMemoryKeyValueStore(), clock: TestClock())
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: true))
        let pending = try await outbox.pendingReadState(forNewsItem: "a")
        XCTAssertEqual(pending, true)
        let unknown = try await outbox.pendingReadState(forNewsItem: "b")
        XCTAssertNil(unknown)
    }
}

/// An API whose write blocks until the test lets it through, so the window *during* a
/// request can be exercised. Reviewers found a real loss in that window: `drain` removed the
/// head entry by position after the round trip, deleting whatever had coalesced into its
/// place.
actor GatedAPI: MotetAPI {
    private var release: CheckedContinuation<Void, Never>?
    private(set) var sent: [String] = []

    func sentCount() -> Int { sent.count }
    func sentReads() -> [String] { sent }

    func letTheRequestFinish() {
        release?.resume()
        release = nil
    }

    func setNewsItemRead(id: String, read: Bool) async throws -> NewsItemResponse {
        sent.append("\(id):\(read)")
        await withCheckedContinuation { continuation in release = continuation }
        return Fixture.newsItem(id: id, read: read)
    }

    // The rest of the contract is not exercised here.
    func listEpisodes() async throws -> [EpisodeResponse] { [] }
    func episode(id: String) async throws -> EpisodeResponse { Fixture.episode(id: id) }
    func createEpisode(title: String, maxDurationMs: Int) async throws -> EpisodeResponse {
        Fixture.episode()
    }
    func markEpisodeListened(id: String) async throws -> MarkListenedResponse {
        MarkListenedResponse(episodeId: id, newsItemsMarkedRead: 0)
    }
    func listNewsItems() async throws -> [NewsItemResponse] { [] }
    func pasteSource(title: String, text: String) async throws -> SourceItemResponse {
        SourceItemResponse(id: "source", state: "pending", title: title)
    }
    func feedInfo() async throws -> FeedInfoResponse {
        FeedInfoResponse(token: "t", url: "https://example.invalid/feed.xml?token=t")
    }
    nonisolated func audioURL(episodeId: String, feedToken: String) throws -> URL {
        URL(string: "https://example.invalid/\(episodeId)")!
    }
}

final class OutboxConcurrencyTests: XCTestCase {
    func testAWriteMadeWhileAnotherIsInFlightIsNotLost() async throws {
        let api = GatedAPI()
        let outbox = Outbox(store: InMemoryKeyValueStore(), clock: TestClock())
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: true))

        async let draining = outbox.drain(using: api)
        try await waitUntil { await api.sentCount() == 1 }

        // The listener changes their mind while the first request is in flight: the queued
        // entry coalesces, replacing the very one being sent. Removing the head *by
        // position* when the request came back would delete this newer fact instead.
        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: false))
        await api.letTheRequestFinish()

        try await waitUntil { await api.sentCount() == 2 }
        await api.letTheRequestFinish()
        _ = try await draining

        let sent = await api.sentReads()
        XCTAssertEqual(sent, ["a:true", "a:false"], "the last word reaches the server")
        let pending = try await outbox.pending()
        XCTAssertTrue(pending.isEmpty)
    }

    /// Poll rather than sleep, with a bound so a regression fails instead of hanging CI.
    private func waitUntil(
        _ condition: () async -> Bool,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        for _ in 0..<10_000 {
            if await condition() { return }
            await Task.yield()
        }
        XCTFail("condition never became true", file: file, line: line)
    }
}

final class OutboxDurabilityTests: XCTestCase {
    func testAnUnreadableQueueIsQuarantinedRatherThanOverwritten() async throws {
        let store = InMemoryKeyValueStore()
        try store.set(Data("not json".utf8), forKey: "outbox.pending")

        let outbox = Outbox(store: store, clock: TestClock())
        let failure = try await outbox.loadFailureDescription()
        XCTAssertNotNil(failure, "silently starting empty would hide the loss")

        try await outbox.enqueue(.newsItemRead(newsItemId: "a", read: true))
        let quarantined = try store.data(forKey: "outbox.unreadable")
        XCTAssertEqual(quarantined, Data("not json".utf8), "the bytes are still recoverable")
    }

    func testAnUnknownClientsErrorIsRetriedRatherThanDropped() {
        // `send` is generic over `any MotetAPI`; a client throwing something that is not a
        // `MotetError` must not have its writes dropped during an offline stretch.
        XCTAssertTrue(Outbox.shouldRetry(URLError(.notConnectedToInternet)))
        XCTAssertTrue(
            Outbox.shouldRetry(MotetError.decoding("captive portal returned HTML")),
            "the write may well have landed, and both requests are idempotent"
        )
        XCTAssertFalse(Outbox.shouldRetry(MotetError.http(status: 422, detail: "no such item")))
        XCTAssertFalse(Outbox.shouldRetry(MotetError.unauthorized))
    }
}
