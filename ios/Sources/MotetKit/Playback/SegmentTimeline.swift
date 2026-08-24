import Foundation

/// The map from a position in an episode's audio to the news items it has spoken.
///
/// This is the piece that makes invariant 5 — read state is per News Item, synced across
/// audio and visual — true for the audio half. Segments carry `start_ms`, `duration_ms`,
/// and the news item they narrate, so "the listener got to 7:42" resolves to a *set of news
/// items*, which is the same fact the backlog screen writes when you tap `read`.
///
/// It is deliberately pure: no player, no network, no clock. Every rule about what counts
/// as heard is decided here and tested directly.
public struct SegmentTimeline: Hashable, Sendable {
    public struct Entry: Hashable, Sendable {
        public let newsItemId: String
        public let newsItemTitle: String
        public let startMs: Int
        public let durationMs: Int

        public var endMs: Int { startMs + durationMs }

        public init(newsItemId: String, newsItemTitle: String, startMs: Int, durationMs: Int) {
            self.newsItemId = newsItemId
            self.newsItemTitle = newsItemTitle
            self.startMs = startMs
            self.durationMs = durationMs
        }
    }

    public let entries: [Entry]
    public let episodeDurationMs: Int

    /// How much of a segment may go unheard and still count as heard.
    ///
    /// A player rarely lands exactly on a boundary — it reports 119.97s of a 120s segment —
    /// and requiring the last few milliseconds would leave the final item of every episode
    /// permanently unread. Half a second is below the shortest meaningful gap in speech.
    public static let completionToleranceMs = 500

    public init(segments: [SegmentResponse], episodeDurationMs: Int) {
        self.entries = segments
            .map {
                Entry(
                    newsItemId: $0.newsItemId,
                    newsItemTitle: $0.newsItemTitle,
                    startMs: $0.startMs,
                    durationMs: $0.durationMs
                )
            }
            .sorted { $0.startMs < $1.startMs }
        self.episodeDurationMs = episodeDurationMs
    }

    public init(entries: [Entry], episodeDurationMs: Int) {
        self.entries = entries.sorted { $0.startMs < $1.startMs }
        self.episodeDurationMs = episodeDurationMs
    }

    public var isEmpty: Bool { entries.isEmpty }

    /// The segment being spoken at `positionMs`, if any.
    public func entry(at positionMs: Int) -> Entry? {
        entries.last { $0.startMs <= positionMs }.flatMap { candidate in
            positionMs < candidate.endMs + Self.completionToleranceMs ? candidate : nil
        }
    }

    /// The news items whose every segment was actually listened to.
    ///
    /// This is the one the player uses. `newsItemsCompleted(through:)` below is the
    /// weaker, position-only form, kept for the case where all that survives is a resume
    /// point — a first launch after an upgrade, before any coverage was recorded.
    public func newsItemsCompleted(coverage: ListenedCoverage) -> [String] {
        var completed: [String] = []
        var seen = Set<String>()
        for entry in entries where !seen.contains(entry.newsItemId) {
            seen.insert(entry.newsItemId)
            let heard = entries
                .filter { $0.newsItemId == entry.newsItemId }
                .allSatisfy {
                    coverage.covers(
                        $0.startMs..<$0.endMs, tolerance: Self.completionToleranceMs
                    )
                }
            if heard { completed.append(entry.newsItemId) }
        }
        return completed
    }

    /// The news items fully spoken by `positionMs`.
    ///
    /// A news item can be narrated by more than one segment; it counts as heard only when
    /// **every** one of its segments has been. Marking a story read halfway through it is
    /// the failure this guards against — the backlog is the product's memory.
    public func newsItemsCompleted(through positionMs: Int) -> [String] {
        var completed: [String] = []
        var seen = Set<String>()
        for entry in entries where !seen.contains(entry.newsItemId) {
            let segmentsForItem = entries.filter { $0.newsItemId == entry.newsItemId }
            let allHeard = segmentsForItem.allSatisfy {
                positionMs + Self.completionToleranceMs >= $0.endMs
            }
            if allHeard {
                completed.append(entry.newsItemId)
                seen.insert(entry.newsItemId)
            }
        }
        return completed
    }

    /// Where the previous segment starts, for a "back to the start of this story" command.
    public func startOfPreviousEntry(from positionMs: Int) -> Int? {
        guard let current = entry(at: positionMs) else {
            return entries.last(where: { $0.startMs < positionMs })?.startMs
        }
        // Within the first second of a segment, "previous" means the one before it;
        // later in the segment it means the start of this one. That is how every podcast
        // client behaves, and it is what makes the button usable without looking.
        if positionMs - current.startMs > 1_000 {
            return current.startMs
        }
        return entries.last(where: { $0.startMs < current.startMs })?.startMs
    }

    /// Where the next segment starts, for a "skip this story" command.
    public func startOfNextEntry(from positionMs: Int) -> Int? {
        entries.first(where: { $0.startMs > positionMs })?.startMs
    }
}
