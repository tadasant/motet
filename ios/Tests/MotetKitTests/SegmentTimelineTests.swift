import XCTest
@testable import MotetKit

/// The audio half of invariant 5 lives here: position in, news items out.
final class SegmentTimelineTests: XCTestCase {
    private let timeline = Fixture.episode().timeline

    func testNothingIsReadAtTheStart() {
        XCTAssertEqual(timeline.newsItemsCompleted(through: 0), [])
    }

    func testAStoryIsNotReadUntilAllOfItsSegmentsAreHeard() {
        // "Alpha" is narrated by two segments, 0–60s and 60–90s. Hearing the first is not
        // hearing the story — marking it read there is the failure this guards against.
        XCTAssertEqual(timeline.newsItemsCompleted(through: 60_000), [])
        XCTAssertEqual(timeline.newsItemsCompleted(through: 89_000), [])
        XCTAssertEqual(timeline.newsItemsCompleted(through: 90_000), ["news-a"])
    }

    func testCompletionToleranceCoversAPlayerThatStopsJustShort() {
        // A player reports 89.7s of a 90s boundary and stops there at the end of a file.
        XCTAssertEqual(timeline.newsItemsCompleted(through: 89_700), ["news-a"])
    }

    func testEverythingIsReadAtTheEnd() {
        XCTAssertEqual(
            timeline.newsItemsCompleted(through: 300_000), ["news-a", "news-b", "news-c"]
        )
    }

    func testCurrentSegmentTracksPosition() {
        XCTAssertEqual(timeline.entry(at: 0)?.newsItemTitle, "Alpha")
        XCTAssertEqual(timeline.entry(at: 95_000)?.newsItemTitle, "Bravo")
        XCTAssertEqual(timeline.entry(at: 299_000)?.newsItemTitle, "Charlie")
    }

    func testNextSegmentStart() {
        XCTAssertEqual(timeline.startOfNextEntry(from: 0), 60_000)
        XCTAssertEqual(timeline.startOfNextEntry(from: 95_000), 180_000)
        XCTAssertNil(timeline.startOfNextEntry(from: 299_000))
    }

    func testPreviousGoesToTheStartOfThisSegmentThenTheOneBefore() {
        // Well inside a segment: back to the start of it.
        XCTAssertEqual(timeline.startOfPreviousEntry(from: 120_000), 90_000)
        // Just after a boundary: back past it, the way every podcast client behaves.
        XCTAssertEqual(timeline.startOfPreviousEntry(from: 90_500), 60_000)
        XCTAssertNil(timeline.startOfPreviousEntry(from: 500))
    }

    func testSegmentsAreSortedRegardlessOfServerOrder() {
        let shuffled = SegmentTimeline(
            segments: [
                Fixture.segment(newsItemId: "b", title: "B", startMs: 10_000, durationMs: 5_000),
                Fixture.segment(newsItemId: "a", title: "A", startMs: 0, durationMs: 10_000),
            ],
            episodeDurationMs: 15_000
        )
        XCTAssertEqual(shuffled.entries.map(\.startMs), [0, 10_000])
    }
}
