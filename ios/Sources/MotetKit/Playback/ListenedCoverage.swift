import Foundation

/// Which parts of an episode were actually *listened to*, as merged half-open ranges.
///
/// Read state cannot be a high-water mark. "The playhead reached 12:00" and "the listener
/// heard the first twelve minutes" are different claims, and a skip button makes them
/// diverge: pressing *next story* three times moves the playhead past three stories without
/// speaking a word of them. Marking those read would quietly empty the backlog, which is the
/// product's memory (invariant 5).
///
/// So the controller accumulates the intervals the audio genuinely played, and a news item
/// counts as heard only when the ranges its segments occupy are covered.
public struct ListenedCoverage: Codable, Hashable, Sendable {
    /// Sorted, disjoint, half-open.
    public private(set) var ranges: [Range<Int>]

    public init(ranges: [Range<Int>] = []) {
        self.ranges = []
        for range in ranges { add(from: range.lowerBound, to: range.upperBound) }
    }

    public var isEmpty: Bool { ranges.isEmpty }

    /// The furthest point covered, for a resume that has nothing better to go on.
    public var upperBound: Int { ranges.last?.upperBound ?? 0 }

    /// Record that `from..<to` was played. Out-of-order and overlapping calls are fine.
    public mutating func add(from: Int, to: Int) {
        let lower = max(0, min(from, to))
        let upper = max(0, max(from, to))
        guard upper > lower else { return }

        var merged: [Range<Int>] = []
        var candidate = lower..<upper
        for range in ranges {
            if range.upperBound < candidate.lowerBound {
                merged.append(range)
            } else if range.lowerBound > candidate.upperBound {
                merged.append(candidate)
                candidate = range
            } else {
                candidate = min(range.lowerBound, candidate.lowerBound)
                    ..< max(range.upperBound, candidate.upperBound)
            }
        }
        merged.append(candidate)
        ranges = merged
    }

    /// How much of `range` is covered.
    public func coveredLength(of range: Range<Int>) -> Int {
        ranges.reduce(0) { total, covered in
            let lower = max(covered.lowerBound, range.lowerBound)
            let upper = min(covered.upperBound, range.upperBound)
            return total + max(0, upper - lower)
        }
    }

    /// Whether `range` was heard, allowing `tolerance` milliseconds of it to be missing.
    ///
    /// The tolerance is not slack for skipping: it absorbs the fact that a player reports
    /// 119.97s of a 120s segment and that the last tick before a boundary lands short.
    public func covers(_ range: Range<Int>, tolerance: Int) -> Bool {
        guard range.upperBound > range.lowerBound else { return true }
        let needed = (range.upperBound - range.lowerBound) - tolerance
        return coveredLength(of: range) >= max(0, needed)
    }
}
