import XCTest
@testable import MotetKit

/// Invariant 4: we own the position, and it outlives the process.
final class ListeningPositionStoreTests: XCTestCase {
    func testPositionSurvivesRelaunch() async throws {
        let directory = Fixture.temporaryDirectory(self)
        let store = try FileKeyValueStore(directory: directory)

        let first = ListeningPositionStore(store: store, clock: TestClock())
        try await first.record(episodeId: "ep-1", spokenThroughMs: 42_000, durationMs: 300_000)

        let second = ListeningPositionStore(store: try FileKeyValueStore(directory: directory))
        let position = try await second.position(for: "ep-1")
        XCTAssertEqual(position?.spokenThroughMs, 42_000)
    }

    func testSeekingBackMovesThePositionButNotTheFurthestPoint() async throws {
        let store = ListeningPositionStore(store: InMemoryKeyValueStore(), clock: TestClock())
        try await store.record(episodeId: "ep-1", spokenThroughMs: 200_000, durationMs: 300_000)
        let after = try await store.record(episodeId: "ep-1", spokenThroughMs: 10_000, durationMs: 300_000)

        XCTAssertEqual(after.spokenThroughMs, 10_000, "resume where the listener is")
        XCTAssertEqual(after.furthestSpokenMs, 200_000, "already heard stays heard")
    }

    func testPositionIsClampedToTheEpisode() async throws {
        let store = ListeningPositionStore(store: InMemoryKeyValueStore(), clock: TestClock())
        let over = try await store.record(episodeId: "ep-1", spokenThroughMs: 999_999, durationMs: 300_000)
        XCTAssertEqual(over.spokenThroughMs, 300_000)
        let under = try await store.record(episodeId: "ep-1", spokenThroughMs: -5, durationMs: 300_000)
        XCTAssertEqual(under.spokenThroughMs, 0)
    }

    func testFinishedStaysFinished() async throws {
        let store = ListeningPositionStore(store: InMemoryKeyValueStore(), clock: TestClock())
        try await store.record(episodeId: "ep-1", spokenThroughMs: 300_000, durationMs: 300_000, finished: true)
        let reopened = try await store.record(episodeId: "ep-1", spokenThroughMs: 0, durationMs: 300_000)
        XCTAssertTrue(reopened.isFinished)
    }
}
