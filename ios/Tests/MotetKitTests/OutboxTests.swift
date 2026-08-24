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
