import Foundation

/// How far back a mailbox's first sync reaches — the one number a person has to be shown.
///
/// motet#139: the first production connect searched a week, found 55 of the ~200 messages in
/// the label, paged through them correctly and said "caught up". Nothing on any screen said
/// "a week", so the cap read as a pagination bug. These are the choices the connect sheet
/// offers and the ones "Sync further back" re-offers.
///
/// In MotetKit rather than in the app, for `SourceStatus`'s reason: it is a rule with an
/// answer worth asserting, and the app target has no tests.
public enum FirstSyncWindow: Sendable {
    /// `motet_sources.gmail.DEFAULT_FIRST_SYNC_DAYS` — what a connect sheet starts on.
    public static let defaultDays = 30

    /// `motet_sources.gmail.MAX_FIRST_SYNC_DAYS` — ten years, offered as "Everything".
    ///
    /// Past the age of any mailbox this product is for, so it is "everything" without being
    /// unbounded. The API refuses anything larger; the worker clamps whatever it reads.
    public static let maxDays = 3650

    public struct Choice: Identifiable, Hashable, Sendable {
        public let days: Int
        public let label: String
        public var id: Int { days }
    }

    public static let choices: [Choice] = [
        Choice(days: 7, label: "Last 7 days"),
        Choice(days: defaultDays, label: "Last 30 days"),
        Choice(days: 90, label: "Last 90 days"),
        Choice(days: 365, label: "Last year"),
        Choice(days: maxDays, label: "Everything"),
    ]

    /// A window as a phrase, for a source that already has one.
    ///
    /// `nil` is a source connected before the window was a choice — and the API deliberately
    /// does not guess what the deployment's fallback is, because that variable is the
    /// worker's and the API cannot read it. So the phrase says exactly that rather than a
    /// number nobody can stand behind.
    public static func label(days: Int?) -> String {
        guard let days else { return "the deployment default" }
        if let known = choices.first(where: { $0.days == days }) {
            return known.label.replacingOccurrences(of: "Last ", with: "the last ")
                .replacingOccurrences(of: "Everything", with: "everything")
        }
        return "the last \(days) days"
    }

    /// The window a "sync further back" control should start on for this source: its own
    /// choice, or the default when nobody has made one.
    public static func starting(from source: SourceResponse) -> Int {
        source.configuredFirstSyncDays ?? defaultDays
    }
}
