// Generated from openapi.yaml by ios/tools/generate_swift_client.py — do not edit by hand.
//
// Regenerate with `bin/generate-ios-client`. `bin/ci` regenerates it and fails on any
// diff, so this file, openapi.yaml, and the FastAPI app cannot drift apart.

import Foundation

// MARK: - Schemas

/// A reported assertion beside the span it came from (invariant 3).
///
/// ``text`` is what gets spoken and may paraphrase; ``source_excerpt`` is the source text
/// the span actually covers, resolved server-side. Both are sent because the episode
/// screen shows them side by side — that display *is* the trust surface, and a client
/// that had to fetch the source separately to render it would sometimes not bother.
public struct ClaimModel: Codable, Hashable, Sendable {
    public var sourceExcerpt: String
    public var sourceTitle: String
    public var span: SourceSpanModel
    public var text: String

    public init(sourceExcerpt: String, sourceTitle: String, span: SourceSpanModel, text: String) {
        self.sourceExcerpt = sourceExcerpt
        self.sourceTitle = sourceTitle
        self.span = span
        self.text = text
    }

    private enum CodingKeys: String, CodingKey {
        case sourceExcerpt = "source_excerpt"
        case sourceTitle = "source_title"
        case span
        case text
    }
}

/// Begin connecting a mailbox. Returns a URL for the user to visit.
public struct ConnectSourceRequest: Codable, Hashable, Sendable {
    public var name: String?
    public var provider: String?
    public var query: String?
    public var redirectUri: String

    public init(
        name: String? = nil,
        provider: String? = nil,
        query: String? = nil,
        redirectUri: String
    ) {
        self.name = name
        self.provider = provider
        self.query = query
        self.redirectUri = redirectUri
    }

    private enum CodingKeys: String, CodingKey {
        case name
        case provider
        case query
        case redirectUri = "redirect_uri"
    }
}

/// Where to send the user, and the source the grant will attach to.
public struct ConnectSourceResponse: Codable, Hashable, Sendable {
    public var authorizationUrl: String
    public var sourceId: String
    public var state: String

    public init(authorizationUrl: String, sourceId: String, state: String) {
        self.authorizationUrl = authorizationUrl
        self.sourceId = sourceId
        self.state = state
    }

    private enum CodingKeys: String, CodingKey {
        case authorizationUrl = "authorization_url"
        case sourceId = "source_id"
        case state
    }
}

/// Phase 1 has manual episodes only: 'all unread', capped by duration.
public struct CreateEpisodeRequest: Codable, Hashable, Sendable {
    public var maxDurationMs: Int
    public var title: String

    public init(maxDurationMs: Int, title: String) {
        self.maxDurationMs = maxDurationMs
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case maxDurationMs = "max_duration_ms"
        case title
    }
}

/// An episode whose stories are selected by a rule rather than by 'all unread'.
public struct CreateSmartEpisodeRequest: Codable, Hashable, Sendable {
    public var maxDurationMs: Int
    public var rule: SmartRuleModel?
    public var title: String

    public init(maxDurationMs: Int, rule: SmartRuleModel? = nil, title: String) {
        self.maxDurationMs = maxDurationMs
        self.rule = rule
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case maxDurationMs = "max_duration_ms"
        case rule
        case title
    }
}

public struct EpisodeResponse: Codable, Hashable, Sendable {
    public var audioBytes: Int?
    public var audioMediaType: String?
    public var createdAt: Date
    public var durationMs: Int
    public var id: String
    public var lastError: String?
    public var maxDurationMs: Int
    public var publishedAt: Date?
    public var segments: [SegmentResponse]
    public var state: String
    public var title: String

    public init(
        audioBytes: Int? = nil,
        audioMediaType: String? = nil,
        createdAt: Date,
        durationMs: Int,
        id: String,
        lastError: String? = nil,
        maxDurationMs: Int,
        publishedAt: Date? = nil,
        segments: [SegmentResponse],
        state: String,
        title: String
    ) {
        self.audioBytes = audioBytes
        self.audioMediaType = audioMediaType
        self.createdAt = createdAt
        self.durationMs = durationMs
        self.id = id
        self.lastError = lastError
        self.maxDurationMs = maxDurationMs
        self.publishedAt = publishedAt
        self.segments = segments
        self.state = state
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case audioBytes = "audio_bytes"
        case audioMediaType = "audio_media_type"
        case createdAt = "created_at"
        case durationMs = "duration_ms"
        case id
        case lastError = "last_error"
        case maxDurationMs = "max_duration_ms"
        case publishedAt = "published_at"
        case segments
        case state
        case title
    }
}

/// The private feed URL, ready to paste into a podcast client.
///
/// The token is returned in full rather than masked. It has to be: a feed URL is copied
/// to a new device months after it was minted, and a secret the owner cannot read back is
/// one that forces a rotation — which unsubscribes every client already using it.
public struct FeedInfoResponse: Codable, Hashable, Sendable {
    public var token: String
    public var url: String

    public init(token: String, url: String) {
        self.token = token
        self.url = url
    }
}

public struct HTTPValidationError: Codable, Hashable, Sendable {
    public var detail: [ValidationError]?

    public init(detail: [ValidationError]? = nil) {
        self.detail = detail
    }
}

/// Liveness plus enough wiring detail to tell 'quiet' from 'unmonitored'.
public struct HealthResponse: Codable, Hashable, Sendable {
    public var authenticated: Bool
    public var errorsConfigured: Bool
    public var inferenceMode: String
    public var service: String
    public var status: String
    public var telemetryConfigured: Bool

    public init(
        authenticated: Bool,
        errorsConfigured: Bool,
        inferenceMode: String,
        service: String,
        status: String,
        telemetryConfigured: Bool
    ) {
        self.authenticated = authenticated
        self.errorsConfigured = errorsConfigured
        self.inferenceMode = inferenceMode
        self.service = service
        self.status = status
        self.telemetryConfigured = telemetryConfigured
    }

    private enum CodingKeys: String, CodingKey {
        case authenticated
        case errorsConfigured = "errors_configured"
        case inferenceMode = "inference_mode"
        case service
        case status
        case telemetryConfigured = "telemetry_configured"
    }
}

/// A saved passage, anchored to the span of source text it quotes.
///
/// The anchor is the source span and nothing else — claims are rewritten on every script
/// retry and audio offsets move on every re-render, while a source item's text never
/// changes. ``episode_id`` and ``anchor_ms`` say where the listener was when they saved
/// it: provenance, not the anchor.
public struct HighlightResponse: Codable, Hashable, Sendable {
    public var anchorMs: Int?
    public var createdAt: Date
    public var episodeId: String?
    public var id: String
    public var newsItemId: String
    public var note: String?
    public var quote: String
    public var sourceItemId: String
    public var span: SourceSpanModel

    public init(
        anchorMs: Int? = nil,
        createdAt: Date,
        episodeId: String? = nil,
        id: String,
        newsItemId: String,
        note: String? = nil,
        quote: String,
        sourceItemId: String,
        span: SourceSpanModel
    ) {
        self.anchorMs = anchorMs
        self.createdAt = createdAt
        self.episodeId = episodeId
        self.id = id
        self.newsItemId = newsItemId
        self.note = note
        self.quote = quote
        self.sourceItemId = sourceItemId
        self.span = span
    }

    private enum CodingKeys: String, CodingKey {
        case anchorMs = "anchor_ms"
        case createdAt = "created_at"
        case episodeId = "episode_id"
        case id
        case newsItemId = "news_item_id"
        case note
        case quote
        case sourceItemId = "source_item_id"
        case span
    }
}

/// How far into an episode the listener has got.
///
/// Invariant 4: we own playback position, so this is a *report* from a client that we
/// record, never a value read back out of a vendor SDK. Invariant 5 is what it does: a
/// story whose segment has been passed is marked read, which is the same fact the backlog
/// screen's toggle writes.
public struct ListenProgressRequest: Codable, Hashable, Sendable {
    public var listenedThroughMs: Int

    public init(listenedThroughMs: Int) {
        self.listenedThroughMs = listenedThroughMs
    }

    private enum CodingKeys: String, CodingKey {
        case listenedThroughMs = "listened_through_ms"
    }
}

public struct ListenProgressResponse: Codable, Hashable, Sendable {
    public var episodeId: String
    public var listenedThroughMs: Int
    public var newsItemsMarkedRead: Int

    public init(episodeId: String, listenedThroughMs: Int, newsItemsMarkedRead: Int) {
        self.episodeId = episodeId
        self.listenedThroughMs = listenedThroughMs
        self.newsItemsMarkedRead = newsItemsMarkedRead
    }

    private enum CodingKeys: String, CodingKey {
        case episodeId = "episode_id"
        case listenedThroughMs = "listened_through_ms"
        case newsItemsMarkedRead = "news_items_marked_read"
    }
}

/// The result of "I listened to this" — read state, synced (invariant 5).
public struct MarkListenedResponse: Codable, Hashable, Sendable {
    public var episodeId: String
    public var newsItemsMarkedRead: Int

    public init(episodeId: String, newsItemsMarkedRead: Int) {
        self.episodeId = episodeId
        self.newsItemsMarkedRead = newsItemsMarkedRead
    }

    private enum CodingKeys: String, CodingKey {
        case episodeId = "episode_id"
        case newsItemsMarkedRead = "news_items_marked_read"
    }
}

/// A deduped story. Read state lives here, per invariant 5 — not per episode.
public struct NewsItemResponse: Codable, Hashable, Sendable {
    public var createdAt: Date
    public var id: String
    public var read: Bool
    public var sourceItemIds: [String]
    public var summary: String
    public var title: String

    public init(
        createdAt: Date,
        id: String,
        read: Bool,
        sourceItemIds: [String],
        summary: String,
        title: String
    ) {
        self.createdAt = createdAt
        self.id = id
        self.read = read
        self.sourceItemIds = sourceItemIds
        self.summary = summary
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case createdAt = "created_at"
        case id
        case read
        case sourceItemIds = "source_item_ids"
        case summary
        case title
    }
}

/// What the provider redirected back with.
public struct OAuthCallbackRequest: Codable, Hashable, Sendable {
    public var code: String
    public var state: String

    public init(code: String, state: String) {
        self.code = code
        self.state = state
    }
}

/// A blob of text pasted in by hand — Phase 1's only ingestion route.
public struct PasteRequest: Codable, Hashable, Sendable {
    public var text: String
    public var title: String

    public init(text: String, title: String) {
        self.text = text
        self.title = title
    }
}

/// Mark one news item read or unread.
///
/// A body rather than two endpoints, because "unread" is a real thing a user wants: the
/// backlog is the product's memory, and being unable to put something back is worse than
/// never having marked it.
public struct ReadStateRequest: Codable, Hashable, Sendable {
    public var read: Bool

    public init(read: Bool) {
        self.read = read
    }
}

/// Save a passage. The platform tool `save_highlight` posts exactly this.
public struct SaveHighlightRequest: Codable, Hashable, Sendable {
    public var anchorMs: Int?
    public var episodeId: String?
    public var newsItemId: String
    public var note: String?
    public var sourceItemId: String
    public var spanEnd: Int
    public var spanStart: Int

    public init(
        anchorMs: Int? = nil,
        episodeId: String? = nil,
        newsItemId: String,
        note: String? = nil,
        sourceItemId: String,
        spanEnd: Int,
        spanStart: Int
    ) {
        self.anchorMs = anchorMs
        self.episodeId = episodeId
        self.newsItemId = newsItemId
        self.note = note
        self.sourceItemId = sourceItemId
        self.spanEnd = spanEnd
        self.spanStart = spanStart
    }

    private enum CodingKeys: String, CodingKey {
        case anchorMs = "anchor_ms"
        case episodeId = "episode_id"
        case newsItemId = "news_item_id"
        case note
        case sourceItemId = "source_item_id"
        case spanEnd = "span_end"
        case spanStart = "span_start"
    }
}

public struct SegmentResponse: Codable, Hashable, Sendable {
    public var claims: [ClaimModel]
    public var durationMs: Int
    public var newsItemId: String
    public var newsItemTitle: String
    public var startMs: Int
    public var text: String

    public init(
        claims: [ClaimModel],
        durationMs: Int,
        newsItemId: String,
        newsItemTitle: String,
        startMs: Int,
        text: String
    ) {
        self.claims = claims
        self.durationMs = durationMs
        self.newsItemId = newsItemId
        self.newsItemTitle = newsItemTitle
        self.startMs = startMs
        self.text = text
    }

    private enum CodingKeys: String, CodingKey {
        case claims
        case durationMs = "duration_ms"
        case newsItemId = "news_item_id"
        case newsItemTitle = "news_item_title"
        case startMs = "start_ms"
        case text
    }
}

/// Filter, window, duration, ranking — how a smart episode chooses its stories.
///
/// Duration is deliberately absent: it is ``max_duration_ms`` on the episode itself. Two
/// copies of a cap is one too many, and the stale one is the one somebody would trust.
public struct SmartRuleModel: Codable, Hashable, Sendable {
    public var maxItems: Int?
    public var ranking: String?
    public var sourceIds: [String]?
    public var unreadOnly: Bool?
    public var windowDays: Int?

    public init(
        maxItems: Int? = nil,
        ranking: String? = nil,
        sourceIds: [String]? = nil,
        unreadOnly: Bool? = nil,
        windowDays: Int? = nil
    ) {
        self.maxItems = maxItems
        self.ranking = ranking
        self.sourceIds = sourceIds
        self.unreadOnly = unreadOnly
        self.windowDays = windowDays
    }

    private enum CodingKeys: String, CodingKey {
        case maxItems = "max_items"
        case ranking
        case sourceIds = "source_ids"
        case unreadOnly = "unread_only"
        case windowDays = "window_days"
    }
}

public struct SourceItemResponse: Codable, Hashable, Sendable {
    public var id: String
    public var state: String
    public var title: String

    public init(id: String, state: String, title: String) {
        self.id = id
        self.state = state
        self.title = title
    }
}

/// A place source items come from — pasted text, or a connected mailbox.
public struct SourceResponse: Codable, Hashable, Sendable {
    public var active: Bool
    public var connected: Bool
    public var createdAt: Date
    public var id: String
    public var kind: String
    public var lastError: String?
    public var lastPolledAt: Date?
    public var name: String
    public var scopes: [String]

    public init(
        active: Bool,
        connected: Bool,
        createdAt: Date,
        id: String,
        kind: String,
        lastError: String? = nil,
        lastPolledAt: Date? = nil,
        name: String,
        scopes: [String]
    ) {
        self.active = active
        self.connected = connected
        self.createdAt = createdAt
        self.id = id
        self.kind = kind
        self.lastError = lastError
        self.lastPolledAt = lastPolledAt
        self.name = name
        self.scopes = scopes
    }

    private enum CodingKeys: String, CodingKey {
        case active
        case connected
        case createdAt = "created_at"
        case id
        case kind
        case lastError = "last_error"
        case lastPolledAt = "last_polled_at"
        case name
        case scopes
    }
}

/// A half-open character range in a source item — what makes a claim checkable.
public struct SourceSpanModel: Codable, Hashable, Sendable {
    public var end: Int
    public var sourceItemId: String
    public var start: Int

    public init(end: Int, sourceItemId: String, start: Int) {
        self.end = end
        self.sourceItemId = sourceItemId
        self.start = start
    }

    private enum CodingKeys: String, CodingKey {
        case end
        case sourceItemId = "source_item_id"
        case start
    }
}

public struct ValidationError: Codable, Hashable, Sendable {
    public var ctx: JSONValue?
    public var input: JSONValue?
    public var loc: [JSONValue]
    public var msg: String
    public var type: String

    public init(
        ctx: JSONValue? = nil,
        input: JSONValue? = nil,
        loc: [JSONValue],
        msg: String,
        type: String
    ) {
        self.ctx = ctx
        self.input = input
        self.loc = loc
        self.msg = msg
        self.type = type
    }
}

// MARK: - Endpoints

/// Every operation in the contract, as a method, a path, and its query.
///
/// Header parameters are absent on purpose: `authorization` is applied centrally by
/// `MotetHTTPClient`, so no call site can pass the wrong token.
public enum MotetEndpoints {
    /// `GET /feed.xml` — Rss Feed
    public static func rssFeed(token: String? = nil) -> HTTPEndpoint {
        var query: [String: String] = [:]
        if let token { query["token"] = String(describing: token) }
        return HTTPEndpoint(method: "GET", path: "/feed.xml", query: query)
    }

    /// `GET /internal/health` — Health
    public static var health: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/internal/health")
    }

    /// `GET /v1/episodes` — List Episodes
    public static var listEpisodes: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/episodes")
    }

    /// `POST /v1/episodes` — Create Episode
    public static var createEpisode: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/episodes")
    }

    /// `POST /v1/episodes/smart` — Create Smart Episode
    public static var createSmartEpisode: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/episodes/smart")
    }

    /// `GET /v1/episodes/{episode_id}` — Get Episode
    public static func getEpisode(episodeId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/episodes/\(MotetPathComponent(episodeId))")
    }

    /// `GET /v1/episodes/{episode_id}/audio` — Episode Audio
    public static func episodeAudio(episodeId: String, token: String? = nil) -> HTTPEndpoint {
        var query: [String: String] = [:]
        if let token { query["token"] = String(describing: token) }
        return HTTPEndpoint(method: "GET", path: "/v1/episodes/\(MotetPathComponent(episodeId))/audio", query: query)
    }

    /// `GET /v1/episodes/{episode_id}/chapters.json` — Episode Chapters
    public static func episodeChapters(episodeId: String, token: String? = nil) -> HTTPEndpoint {
        var query: [String: String] = [:]
        if let token { query["token"] = String(describing: token) }
        return HTTPEndpoint(method: "GET", path: "/v1/episodes/\(MotetPathComponent(episodeId))/chapters.json", query: query)
    }

    /// `POST /v1/episodes/{episode_id}/listened` — Mark Episode Listened
    public static func markEpisodeListened(episodeId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/episodes/\(MotetPathComponent(episodeId))/listened")
    }

    /// `POST /v1/episodes/{episode_id}/progress` — Report Listen Progress
    public static func reportListenProgress(episodeId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/episodes/\(MotetPathComponent(episodeId))/progress")
    }

    /// `GET /v1/episodes/{episode_id}/transcript.vtt` — Episode Transcript
    public static func episodeTranscript(episodeId: String, token: String? = nil) -> HTTPEndpoint {
        var query: [String: String] = [:]
        if let token { query["token"] = String(describing: token) }
        return HTTPEndpoint(method: "GET", path: "/v1/episodes/\(MotetPathComponent(episodeId))/transcript.vtt", query: query)
    }

    /// `GET /v1/feed` — Get Feed Info
    public static var getFeedInfo: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/feed")
    }

    /// `POST /v1/feed/rotate` — Rotate Feed
    public static var rotateFeed: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/feed/rotate")
    }

    /// `GET /v1/highlights` — List Highlights
    public static var listHighlights: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/highlights")
    }

    /// `POST /v1/highlights` — Save Highlight
    public static var saveHighlight: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/highlights")
    }

    /// `DELETE /v1/highlights/{highlight_id}` — Delete Highlight
    public static func deleteHighlight(highlightId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "DELETE", path: "/v1/highlights/\(MotetPathComponent(highlightId))")
    }

    /// `GET /v1/news-items` — List News Items
    public static var listNewsItems: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/news-items")
    }

    /// `POST /v1/news-items/{news_item_id}/read` — Set News Item Read
    public static func setNewsItemRead(newsItemId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/news-items/\(MotetPathComponent(newsItemId))/read")
    }

    /// `GET /v1/sources` — List Sources
    public static var listSources: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/sources")
    }

    /// `POST /v1/sources/callback` — Oauth Callback
    public static var oauthCallback: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/sources/callback")
    }

    /// `POST /v1/sources/connect` — Connect Source
    public static var connectSource: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/sources/connect")
    }

    /// `POST /v1/sources/paste` — Paste Source
    public static var pasteSource: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/sources/paste")
    }

    /// `DELETE /v1/sources/{source_id}/credentials` — Disconnect Source
    public static func disconnectSource(sourceId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "DELETE", path: "/v1/sources/\(MotetPathComponent(sourceId))/credentials")
    }

    /// `POST /v1/sources/{source_id}/poll` — Poll Source
    public static func pollSource(sourceId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/sources/\(MotetPathComponent(sourceId))/poll")
    }
}
