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

    /// `GET /healthz` — Healthz
    public static var healthz: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/healthz")
    }

    /// `GET /v1/episodes` — List Episodes
    public static var listEpisodes: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/episodes")
    }

    /// `POST /v1/episodes` — Create Episode
    public static var createEpisode: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/episodes")
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

    /// `POST /v1/episodes/{episode_id}/listened` — Mark Episode Listened
    public static func markEpisodeListened(episodeId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/episodes/\(MotetPathComponent(episodeId))/listened")
    }

    /// `GET /v1/feed` — Get Feed Info
    public static var getFeedInfo: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/feed")
    }

    /// `POST /v1/feed/rotate` — Rotate Feed
    public static var rotateFeed: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/feed/rotate")
    }

    /// `GET /v1/news-items` — List News Items
    public static var listNewsItems: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/news-items")
    }

    /// `POST /v1/news-items/{news_item_id}/read` — Set News Item Read
    public static func setNewsItemRead(newsItemId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/news-items/\(MotetPathComponent(newsItemId))/read")
    }

    /// `POST /v1/sources/paste` — Paste Source
    public static var pasteSource: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/sources/paste")
    }
}
