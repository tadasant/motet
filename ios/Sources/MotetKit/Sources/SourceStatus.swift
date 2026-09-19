import Foundation

/// The pure half of the Sources screen: what a row means, and what a source has pulled in.
///
/// A port of the SPA's `screens/sources/status.ts` and `catalog.ts` (motet#90), kept in
/// MotetKit so the rules are tested on Linux and the two clients give one reading per row.
/// Where the SPA's copy changes, change this with it.
public enum Integration: String, CaseIterable, Hashable, Sendable, Identifiable {
    case gmail
    case paste
    case xBookmarks = "x"
    case rss

    public var id: String { rawValue }

    public enum Availability: Hashable, Sendable {
        /// Connectable through `/v1/sources/connect`.
        case available
        /// Always present and never connected: the `src_paste` row migration 0002 seeds.
        case builtin
        /// Not built. Shown so the catalog is honest about its edges, never offered.
        case comingSoon
    }

    public var name: String {
        switch self {
        case .gmail: return "Gmail"
        case .paste: return "Paste"
        case .xBookmarks: return "X bookmarks"
        case .rss: return "RSS"
        }
    }

    public var summary: String {
        switch self {
        case .gmail: return "Newsletters from a mailbox, matched by a Gmail search you choose."
        case .paste: return "Text you paste in yourself. Always on; nothing to connect."
        case .xBookmarks: return "Posts you bookmark on X, pulled in as source items."
        case .rss: return "Feeds you subscribe to, polled like a mailbox."
        }
    }

    public var detail: String {
        switch self {
        case .gmail:
            return "Read-only. Motet polls the mailbox, pulls matching messages in and extracts the article from each. Nothing is processed until you ingest it."
        case .paste:
            return "Pasting is asking: a paste is processed at once rather than held, because the act of pasting it is the decision."
        case .xBookmarks: return "Waits on a decision about the X API tier. Not built."
        case .rss: return "No adapter yet."
        }
    }

    /// The `SourceResponse.kind` rows of this integration carry, or nil when nothing can be a row.
    public var kind: String? {
        switch self {
        case .gmail: return "gmail"
        case .paste: return SourceStatus.pasteKind
        case .xBookmarks, .rss: return nil
        }
    }

    public var availability: Availability {
        switch self {
        case .gmail: return .available
        case .paste: return .builtin
        case .xBookmarks, .rss: return .comingSoon
        }
    }

    /// The rows of this integration, out of every source the API listed.
    public func rows(in sources: [SourceResponse]) -> [SourceResponse] {
        guard let kind else { return [] }
        return sources.filter { $0.kind == kind }
    }
}

public enum SourceStatus {
    /// `paste` is the one kind with nothing behind it: no credential and no schedule.
    public static let pasteKind = "paste"

    /// Motet's default Gmail search (`motet_sources.gmail.DEFAULT_QUERY`), shown so the
    /// field reads as "override this".
    public static let defaultQuery = "category:updates OR category:promotions"

    /// Whether anything ever polls it — and whether `connected` says anything about it.
    public static func isPollable(_ source: SourceResponse) -> Bool {
        source.kind != pasteKind
    }

    /// What one source row is doing. See the SPA's `rowStatus` for the long form; the short
    /// one is that an abandoned consent (a row created when Connect was pressed, with no
    /// credential ever arriving) must not read like a broken source.
    public enum Row: String, Hashable, Sendable {
        case connected
        case paused
        case error
        case awaitingConsent = "awaiting_consent"
        case disconnected
        case ready

        /// Order in a list: the one that needs attention first.
        public var rank: Int {
            switch self {
            case .error: return 0
            case .connected: return 1
            case .paused: return 2
            case .disconnected: return 3
            case .awaitingConsent: return 4
            case .ready: return 5
            }
        }

        public var label: String {
            switch self {
            case .connected: return "Connected"
            case .paused: return "Paused"
            case .error: return "Error"
            case .awaitingConsent: return "Awaiting consent"
            case .disconnected: return "Disconnected"
            case .ready: return "Always on"
            }
        }
    }

    public static func row(_ source: SourceResponse) -> Row {
        guard isPollable(source) else { return source.active ? .ready : .paused }
        if !source.connected {
            return source.disconnectedAt != nil || source.lastPolledAt != nil
                ? .disconnected : .awaitingConsent
        }
        if source.lastError != nil { return .error }
        return source.active ? .connected : .paused
    }

    /// Rows in the order a panel shows them.
    public static func ordered(_ sources: [SourceResponse]) -> [SourceResponse] {
        sources.enumerated()
            .sorted { lhs, rhs in
                let (l, r) = (row(lhs.element).rank, row(rhs.element).rank)
                return l == r ? lhs.offset < rhs.offset : l < r
            }
            .map(\.element)
    }

    /// What an integration's card says, folding every row of it into one pill.
    public enum Card: Hashable, Sendable {
        case connected(count: Int)
        case error
        case paused
        case awaitingConsent
        case disconnected
        case notConnected
        case alwaysOn
        case comingSoon

        public var label: String {
            switch self {
            case .connected(let count): return count > 1 ? "\(count) connected" : "Connected"
            case .error: return "Error"
            case .paused: return "Paused"
            case .awaitingConsent: return "Awaiting consent"
            case .disconnected: return "Disconnected"
            case .notConnected: return "Not connected"
            case .alwaysOn: return "Always on"
            case .comingSoon: return "Coming soon"
            }
        }
    }

    public static func card(_ integration: Integration, rows: [SourceResponse]) -> Card {
        switch integration.availability {
        case .comingSoon: return .comingSoon
        case .builtin: return .alwaysOn
        case .available: break
        }
        let statuses = rows.map(row)
        if statuses.contains(.error) { return .error }
        let connected = statuses.filter { $0 == .connected }.count
        if connected > 0 { return .connected(count: connected) }
        if statuses.contains(.paused) { return .paused }
        if statuses.contains(.awaitingConsent) { return .awaitingConsent }
        if statuses.contains(.disconnected) { return .disconnected }
        return .notConnected
    }

    /// What one source has pulled in, and where it is now. Held items are `/v1/source-items/
    /// held`; the rest are `/v1/ingestion`, which leaves held items out (motet#91).
    public struct Counts: Hashable, Sendable {
        public var held = 0
        public var processing = 0
        public var failed = 0
        public var integrated = 0

        public init(held: Int = 0, processing: Int = 0, failed: Int = 0, integrated: Int = 0) {
            self.held = held
            self.processing = processing
            self.failed = failed
            self.integrated = integrated
        }
    }

    public static func counts(
        for source: SourceResponse,
        held: [HeldSourceItemResponse],
        ingestion: [IngestionItemResponse]
    ) -> Counts {
        let here = ingestion.filter { $0.sourceId == source.id }
        return Counts(
            held: held.filter { $0.sourceId == source.id }.count,
            processing: here.filter { $0.state == "pending" }.count,
            failed: here.filter { $0.state == "failed" }.count,
            integrated: here.filter { $0.state == "integrated" }.count
        )
    }

    /// What the most recent poll found, in a sentence. Nil when no poll has run.
    public static func describeLastSync(_ source: SourceResponse) -> String? {
        guard let last = source.lastSync else { return nil }
        if let error = last.error { return "The last sync gave up: \(error)" }
        let plural = last.seen == 1 ? "" : "s"
        let looked: String
        if last.seen == 0 {
            looked = "No new messages."
        } else if last.queued == 0 {
            looked = "Looked at \(last.seen) message\(plural); none were new."
        } else {
            looked = "Looked at \(last.seen) message\(plural); \(last.queued) \(last.queued == 1 ? "was" : "were") new."
        }
        return last.caughtUp ? looked : "\(looked) Still catching up: each sync queues the next."
    }

    /// When the last sync ran, for display. Not what "Sync now" watches — see `syncedAt`.
    public static func lastSyncedAt(_ source: SourceResponse) -> Date? {
        source.lastSync?.at ?? source.lastPolledAt
    }

    /// What "Sync now" watches: only `last_sync.at`, which a poll writes and nothing else
    /// does. `last_polled_at` also moves when extraction skips a message.
    public static func syncedAt(_ source: SourceResponse) -> Date? {
        source.lastSync?.at
    }

    /// OAuth scopes as short names.
    public static func describeScope(_ scope: String) -> String {
        switch scope {
        case "https://www.googleapis.com/auth/gmail.readonly", "gmail.readonly": return "read-only mail"
        case "https://www.googleapis.com/auth/gmail.modify", "gmail.modify": return "label changes"
        case "openid": return "identity"
        case "email": return "email address"
        case "profile": return "profile"
        default: return scope
        }
    }

    /// Whether a worker is draining the queues, from the heartbeat (motet#38). A queued
    /// poll with no worker alive is a poll nothing will run.
    public enum Worker: Hashable, Sendable {
        case running
        case idle
        case never
        /// The route answered nothing. Not "no worker": an outage must not read as idle.
        case unknown
    }

    /// How recent a heartbeat counts as a worker running. The SPA's `WORKER_FRESH_MS`.
    public static let workerFreshSeconds: TimeInterval = 5 * 60

    public static func worker(_ processing: ProcessingStatusResponse?) -> Worker {
        guard let processing else { return .unknown }
        guard let seen = processing.workerLastSeenAt else { return .never }
        return processing.now.timeIntervalSince(seen) <= workerFreshSeconds ? .running : .idle
    }

    public static func describeQueuedSync(_ worker: Worker) -> String {
        switch worker {
        case .running: return "Queued — a worker is running and will pick it up."
        case .unknown: return "Queued. Whether a worker is running could not be checked."
        case .idle, .never:
            return "Queued — but no worker has run in the last five minutes, so nothing will pick it up until one does."
        }
    }

    /// What a label-sync setting does to an ingested item's message, in words.
    public static func describeLabelMove(remove: String?, add: String?) -> String? {
        let remove = remove.flatMap { $0.isEmpty ? nil : $0 }
        let add = add.flatMap { $0.isEmpty ? nil : $0 }
        switch (remove, add) {
        case let (remove?, add?): return "moves its message from \(remove) to \(add)"
        case let (remove?, nil): return "takes \(remove) off its message"
        case let (nil, add?): return "adds \(add) to its message"
        case (nil, nil): return nil
        }
    }
}

/// The Credentials half of the screen: sites and MCP servers enrichment may use (motet#102).
public enum ConnectorStatus {
    public static let siteKind = "site"
    public static let mcpKind = "mcp"

    public enum Pill: Hashable, Sendable {
        case ready(String)
        case needsAuth
        case error

        public var label: String {
            switch self {
            case .ready(let text): return text
            case .needsAuth: return "Needs authorization"
            case .error: return "Error"
            }
        }
    }

    /// The SPA's `ConnectorRow` pill, one reading per row.
    public static func pill(_ connector: ConnectorResponse) -> Pill {
        if connector.status == "error" { return .error }
        if connector.status == "needs_auth" { return .needsAuth }
        if connector.kind == siteKind {
            guard connector.username?.isEmpty == false else { return .ready("Ready · no login") }
            return connector.hasSecret ? .ready("Ready · login saved") : .ready("Ready · passwordless login")
        }
        return .ready("Authorized")
    }

    /// The host part of whatever was typed or pasted: scheme, credentials, `www.`, path and
    /// port gone. The API normalizes again and is the authority; this is so the field shows
    /// `example.com` before Add rather than after. Keep in step with the SPA's
    /// `normalizeDomain` and `motet_db.connectors.normalize_domain`.
    public static func normalizeDomain(_ raw: String) -> String {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if let scheme = value.range(of: "://") { value = String(value[scheme.upperBound...]) }
        if let cut = value.firstIndex(where: { "/?#".contains($0) }) { value = String(value[..<cut]) }
        if let at = value.lastIndex(of: "@") { value = String(value[value.index(after: at)...]) }
        if let colon = value.firstIndex(of: ":") { value = String(value[..<colon]) }
        while value.hasPrefix(".") { value.removeFirst() }
        while value.hasSuffix(".") { value.removeLast() }
        if value.hasPrefix("www.") { value.removeFirst(4) }
        return value
    }

    /// A host name with at least one dot and a letter-led top label — never an IP literal.
    public static func looksLikeDomain(_ value: String) -> Bool {
        let labels = value.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 2 else { return false }
        let allowed = Set("abcdefghijklmnopqrstuvwxyz0123456789-")
        for label in labels {
            guard (1...63).contains(label.count), label.allSatisfy(allowed.contains),
                  label.first != "-", label.last != "-" else { return false }
        }
        return labels.last?.first?.isLetter ?? false
    }

    /// The comma-separated "only for these sites" field, as the request wants it.
    public static func domainList(_ raw: String) -> [String] {
        raw.split(whereSeparator: { $0 == "," || $0.isWhitespace })
            .map { normalizeDomain(String($0)) }
            .filter { !$0.isEmpty }
    }
}
