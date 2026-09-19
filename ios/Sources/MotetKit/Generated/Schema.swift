// Generated from openapi.yaml by ios/tools/generate_swift_client.py — do not edit by hand.
//
// Regenerate with `bin/generate-ios-client`. `bin/ci` regenerates it and fails on any
// diff, so this file, openapi.yaml, and the FastAPI app cannot drift apart.

import Foundation

// MARK: - Schemas

public struct AdminEpisodeCounts: Codable, Hashable, Sendable {
    public var failed: Int
    public var pending: Int
    public var ready: Int
    public var rendering: Int
    public var scripting: Int

    public init(failed: Int, pending: Int, ready: Int, rendering: Int, scripting: Int) {
        self.failed = failed
        self.pending = pending
        self.ready = ready
        self.rendering = rendering
        self.scripting = scripting
    }
}

public struct AdminJobCounts: Codable, Hashable, Sendable {
    public var done: Int
    public var failed: Int
    public var ready: Int
    public var running: Int

    public init(done: Int, failed: Int, ready: Int, running: Int) {
        self.done = done
        self.failed = failed
        self.ready = ready
        self.running = running
    }
}

/// One job row, with its payload resolved to a user and a domain subject.
public struct AdminJobResponse: Codable, Hashable, Sendable {
    public var attempts: Int
    public var createdAt: Date
    public var id: Int
    public var lastError: String?
    public var lockedAt: Date?
    public var queue: String
    public var runAt: Date
    public var state: String
    public var subject: String?
    public var updatedAt: Date
    public var userId: String?

    public init(
        attempts: Int,
        createdAt: Date,
        id: Int,
        lastError: String? = nil,
        lockedAt: Date? = nil,
        queue: String,
        runAt: Date,
        state: String,
        subject: String? = nil,
        updatedAt: Date,
        userId: String? = nil
    ) {
        self.attempts = attempts
        self.createdAt = createdAt
        self.id = id
        self.lastError = lastError
        self.lockedAt = lockedAt
        self.queue = queue
        self.runAt = runAt
        self.state = state
        self.subject = subject
        self.updatedAt = updatedAt
        self.userId = userId
    }

    private enum CodingKeys: String, CodingKey {
        case attempts
        case createdAt = "created_at"
        case id
        case lastError = "last_error"
        case lockedAt = "locked_at"
        case queue
        case runAt = "run_at"
        case state
        case subject
        case updatedAt = "updated_at"
        case userId = "user_id"
    }
}

/// The `llm_usage` ledger: what each stage, user and queue spent. Admins only.
///
/// One row per pipeline completion, written by the worker. Nothing is backfilled, so
/// `since` is when the first retained row landed (null if none), and rows older than
/// `retention_days` are deleted. Voice turns are not in it: the voice service has no
/// database, so its spend is the `motet.llm.tokens` metric only.
public struct AdminLlmSpendResponse: Codable, Hashable, Sendable {
    public var generatedAt: Date
    public var retentionDays: Int
    public var since: Date?
    public var total: LlmSpendBreakdown
    public var window: LlmSpendBreakdown
    public var windowDays: Int

    public init(
        generatedAt: Date,
        retentionDays: Int,
        since: Date? = nil,
        total: LlmSpendBreakdown,
        window: LlmSpendBreakdown,
        windowDays: Int
    ) {
        self.generatedAt = generatedAt
        self.retentionDays = retentionDays
        self.since = since
        self.total = total
        self.window = window
        self.windowDays = windowDays
    }

    private enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case retentionDays = "retention_days"
        case since
        case total
        case window
        case windowDays = "window_days"
    }
}

public struct AdminNewsItemCounts: Codable, Hashable, Sendable {
    public var read: Int
    public var unread: Int

    public init(read: Int, unread: Int) {
        self.read = read
        self.unread = unread
    }
}

/// The whole deployment at a glance, across every user. Admins only.
///
/// Aggregates are always for everyone; only `jobs` is paged, and filtered when a
/// `user_id` is asked for. Every user and every pipeline queue is present, at zero when
/// empty.
public struct AdminOverviewResponse: Codable, Hashable, Sendable {
    public var generatedAt: Date
    public var jobs: [AdminJobResponse]
    public var jobsNextBefore: Int?
    public var queues: [AdminQueueResponse]
    public var users: [AdminUserResponse]

    public init(
        generatedAt: Date,
        jobs: [AdminJobResponse],
        jobsNextBefore: Int? = nil,
        queues: [AdminQueueResponse],
        users: [AdminUserResponse]
    ) {
        self.generatedAt = generatedAt
        self.jobs = jobs
        self.jobsNextBefore = jobsNextBefore
        self.queues = queues
        self.users = users
    }

    private enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case jobs
        case jobsNextBefore = "jobs_next_before"
        case queues
        case users
    }
}

/// One queue's counts per job state, plus the two liveness facts an operator wants.
public struct AdminQueueResponse: Codable, Hashable, Sendable {
    public var done: Int
    public var failed: Int
    public var lastHeartbeatAt: Date?
    public var oldestReadyAgeS: Double?
    public var queue: String
    public var ready: Int
    public var running: Int

    public init(
        done: Int,
        failed: Int,
        lastHeartbeatAt: Date? = nil,
        oldestReadyAgeS: Double? = nil,
        queue: String,
        ready: Int,
        running: Int
    ) {
        self.done = done
        self.failed = failed
        self.lastHeartbeatAt = lastHeartbeatAt
        self.oldestReadyAgeS = oldestReadyAgeS
        self.queue = queue
        self.ready = ready
        self.running = running
    }

    private enum CodingKeys: String, CodingKey {
        case done
        case failed
        case lastHeartbeatAt = "last_heartbeat_at"
        case oldestReadyAgeS = "oldest_ready_age_s"
        case queue
        case ready
        case running
    }
}

public struct AdminSourceItemCounts: Codable, Hashable, Sendable {
    public var dismissed: Int
    public var failed: Int
    public var held: Int
    public var integrated: Int
    public var pending: Int

    public init(dismissed: Int, failed: Int, held: Int, integrated: Int, pending: Int) {
        self.dismissed = dismissed
        self.failed = failed
        self.held = held
        self.integrated = integrated
        self.pending = pending
    }
}

/// One user's counts per state, across every table that carries a `user_id`.
public struct AdminUserResponse: Codable, Hashable, Sendable {
    public var email: String?
    public var episodes: AdminEpisodeCounts
    public var jobs: AdminJobCounts
    public var newsItems: AdminNewsItemCounts
    public var sourceItems: AdminSourceItemCounts
    public var userId: String

    public init(
        email: String? = nil,
        episodes: AdminEpisodeCounts,
        jobs: AdminJobCounts,
        newsItems: AdminNewsItemCounts,
        sourceItems: AdminSourceItemCounts,
        userId: String
    ) {
        self.email = email
        self.episodes = episodes
        self.jobs = jobs
        self.newsItems = newsItems
        self.sourceItems = sourceItems
        self.userId = userId
    }

    private enum CodingKeys: String, CodingKey {
        case email
        case episodes
        case jobs
        case newsItems = "news_items"
        case sourceItems = "source_items"
        case userId = "user_id"
    }
}

public struct AdminUserSpend: Codable, Hashable, Sendable {
    public var email: String?
    public var spend: LlmSpend
    public var userId: String

    public init(email: String? = nil, spend: LlmSpend, userId: String) {
        self.email = email
        self.spend = spend
        self.userId = userId
    }

    private enum CodingKeys: String, CodingKey {
        case email
        case spend
        case userId = "user_id"
    }
}

/// The landing page's waitlist, newest first. Admins only.
public struct AdminWaitlistResponse: Codable, Hashable, Sendable {
    public var nextBefore: Int?
    public var signups: [AdminWaitlistSignupResponse]
    public var total: Int

    public init(nextBefore: Int? = nil, signups: [AdminWaitlistSignupResponse], total: Int) {
        self.nextBefore = nextBefore
        self.signups = signups
        self.total = total
    }

    private enum CodingKeys: String, CodingKey {
        case nextBefore = "next_before"
        case signups
        case total
    }
}

/// One address on the waitlist.
public struct AdminWaitlistSignupResponse: Codable, Hashable, Sendable {
    public var createdAt: Date
    public var email: String
    public var id: Int
    public var lastSubmittedAt: Date
    public var submissions: Int

    public init(createdAt: Date, email: String, id: Int, lastSubmittedAt: Date, submissions: Int) {
        self.createdAt = createdAt
        self.email = email
        self.id = id
        self.lastSubmittedAt = lastSubmittedAt
        self.submissions = submissions
    }

    private enum CodingKeys: String, CodingKey {
        case createdAt = "created_at"
        case email
        case id
        case lastSubmittedAt = "last_submitted_at"
        case submissions
    }
}

public struct AuthorizeConnectorRequest: Codable, Hashable, Sendable {
    public var redirectUri: String

    public init(redirectUri: String) {
        self.redirectUri = redirectUri
    }

    private enum CodingKeys: String, CodingKey {
        case redirectUri = "redirect_uri"
    }
}

public struct AuthorizeConnectorResponse: Codable, Hashable, Sendable {
    public var authorizationUrl: String
    public var state: String

    public init(authorizationUrl: String, state: String) {
        self.authorizationUrl = authorizationUrl
        self.state = state
    }

    private enum CodingKeys: String, CodingKey {
        case authorizationUrl = "authorization_url"
        case state
    }
}

/// A reported assertion beside the span it came from (invariant 3).
///
/// ``text`` is what gets spoken and may paraphrase; ``source_excerpt`` is the source text
/// the span actually covers, resolved server-side. Both are sent because the episode
/// screen shows them side by side — that display *is* the trust surface, and a client
/// that had to fetch the source separately to render it would sometimes not bother.
public struct ClaimModel: Codable, Hashable, Sendable {
    public var durationMs: Int?
    public var sourceExcerpt: String
    public var sourceTitle: String
    public var span: SourceSpanModel
    public var startMs: Int?
    public var text: String

    public init(
        durationMs: Int? = nil,
        sourceExcerpt: String,
        sourceTitle: String,
        span: SourceSpanModel,
        startMs: Int? = nil,
        text: String
    ) {
        self.durationMs = durationMs
        self.sourceExcerpt = sourceExcerpt
        self.sourceTitle = sourceTitle
        self.span = span
        self.startMs = startMs
        self.text = text
    }

    private enum CodingKeys: String, CodingKey {
        case durationMs = "duration_ms"
        case sourceExcerpt = "source_excerpt"
        case sourceTitle = "source_title"
        case span
        case startMs = "start_ms"
        case text
    }
}

/// What Google redirected back with.
public struct CompleteLoginRequest: Codable, Hashable, Sendable {
    public var code: String
    public var state: String

    public init(code: String, state: String) {
        self.code = code
        self.state = state
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

public struct ConnectorOAuthCallbackRequest: Codable, Hashable, Sendable {
    public var code: String
    public var iss: String?
    public var state: String

    public init(code: String, iss: String? = nil, state: String) {
        self.code = code
        self.iss = iss
        self.state = state
    }
}

/// A site or MCP server agentic enrichment may use. **Never carries the secret** —
/// only whether one is stored, answered without decrypting anything.
public struct ConnectorResponse: Codable, Hashable, Sendable {
    public var createdAt: Date
    public var domain: String?
    public var domains: [String]
    public var hasSecret: Bool
    public var id: String
    public var kind: String
    public var label: String
    public var lastError: String?
    public var oauthIssuer: String?
    public var oauthRegistered: Bool
    public var riskAcknowledgedAt: Date?
    public var secretExpiresAt: Date?
    public var status: String
    public var updatedAt: Date
    public var url: String?
    public var username: String?

    public init(
        createdAt: Date,
        domain: String? = nil,
        domains: [String],
        hasSecret: Bool,
        id: String,
        kind: String,
        label: String,
        lastError: String? = nil,
        oauthIssuer: String? = nil,
        oauthRegistered: Bool,
        riskAcknowledgedAt: Date? = nil,
        secretExpiresAt: Date? = nil,
        status: String,
        updatedAt: Date,
        url: String? = nil,
        username: String? = nil
    ) {
        self.createdAt = createdAt
        self.domain = domain
        self.domains = domains
        self.hasSecret = hasSecret
        self.id = id
        self.kind = kind
        self.label = label
        self.lastError = lastError
        self.oauthIssuer = oauthIssuer
        self.oauthRegistered = oauthRegistered
        self.riskAcknowledgedAt = riskAcknowledgedAt
        self.secretExpiresAt = secretExpiresAt
        self.status = status
        self.updatedAt = updatedAt
        self.url = url
        self.username = username
    }

    private enum CodingKeys: String, CodingKey {
        case createdAt = "created_at"
        case domain
        case domains
        case hasSecret = "has_secret"
        case id
        case kind
        case label
        case lastError = "last_error"
        case oauthIssuer = "oauth_issuer"
        case oauthRegistered = "oauth_registered"
        case riskAcknowledgedAt = "risk_acknowledged_at"
        case secretExpiresAt = "secret_expires_at"
        case status
        case updatedAt = "updated_at"
        case url
        case username
    }
}

public struct CreateConnectorRequest: Codable, Hashable, Sendable {
    public var acknowledgeRisk: Bool?
    public var domain: String?
    public var domains: [String]?
    public var kind: String
    public var label: String?
    public var password: String?
    public var url: String?
    public var username: String?

    public init(
        acknowledgeRisk: Bool? = nil,
        domain: String? = nil,
        domains: [String]? = nil,
        kind: String,
        label: String? = nil,
        password: String? = nil,
        url: String? = nil,
        username: String? = nil
    ) {
        self.acknowledgeRisk = acknowledgeRisk
        self.domain = domain
        self.domains = domains
        self.kind = kind
        self.label = label
        self.password = password
        self.url = url
        self.username = username
    }

    private enum CodingKeys: String, CodingKey {
        case acknowledgeRisk = "acknowledge_risk"
        case domain
        case domains
        case kind
        case label
        case password
        case url
        case username
    }
}

/// 'All unread', capped by duration — or exactly the stories somebody picked.
public struct CreateEpisodeRequest: Codable, Hashable, Sendable {
    public var keepInBacklog: Bool?
    public var maxDurationMs: Int
    public var newsItemIds: [String]?
    public var title: String

    public init(
        keepInBacklog: Bool? = nil,
        maxDurationMs: Int,
        newsItemIds: [String]? = nil,
        title: String
    ) {
        self.keepInBacklog = keepInBacklog
        self.maxDurationMs = maxDurationMs
        self.newsItemIds = newsItemIds
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case keepInBacklog = "keep_in_backlog"
        case maxDurationMs = "max_duration_ms"
        case newsItemIds = "news_item_ids"
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

/// Why dedup put this source item where it did, as recorded at the time.
///
/// ``relation``, ``reason``, ``candidate_id`` and ``model`` are the first pass's answer and
/// are null when the integrator reported none. ``basis`` names the step the outcome rests
/// on. ``title`` and ``summary`` are the news item's copy as this decision left it; the
/// news item's own are rewritten by every later merge.
public struct DedupDecisionResponse: Codable, Hashable, Sendable {
    public var basis: String
    public var candidateId: String?
    public var candidateTitle: String?
    public var decidedAt: Date
    public var model: String?
    public var reason: String?
    public var relation: String?
    public var summary: String?
    public var title: String?

    public init(
        basis: String,
        candidateId: String? = nil,
        candidateTitle: String? = nil,
        decidedAt: Date,
        model: String? = nil,
        reason: String? = nil,
        relation: String? = nil,
        summary: String? = nil,
        title: String? = nil
    ) {
        self.basis = basis
        self.candidateId = candidateId
        self.candidateTitle = candidateTitle
        self.decidedAt = decidedAt
        self.model = model
        self.reason = reason
        self.relation = relation
        self.summary = summary
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case basis
        case candidateId = "candidate_id"
        case candidateTitle = "candidate_title"
        case decidedAt = "decided_at"
        case model
        case reason
        case relation
        case summary
        case title
    }
}

public struct DismissResponse: Codable, Hashable, Sendable {
    public var dismissed: Int
    public var skipped: Int

    public init(dismissed: Int, skipped: Int) {
        self.dismissed = dismissed
        self.skipped = skipped
    }
}

/// One agent run: what it cost, how hard it worked, and whether it logged in.
public struct EnrichRunResponse: Codable, Hashable, Sendable {
    public var articleChars: Int
    public var costUsd: Double
    public var error: String?
    public var finishedAt: Date?
    public var id: String
    public var loginPerformed: Bool
    public var startedAt: Date
    public var status: String
    public var toolCalls: Int

    public init(
        articleChars: Int,
        costUsd: Double,
        error: String? = nil,
        finishedAt: Date? = nil,
        id: String,
        loginPerformed: Bool,
        startedAt: Date,
        status: String,
        toolCalls: Int
    ) {
        self.articleChars = articleChars
        self.costUsd = costUsd
        self.error = error
        self.finishedAt = finishedAt
        self.id = id
        self.loginPerformed = loginPerformed
        self.startedAt = startedAt
        self.status = status
        self.toolCalls = toolCalls
    }

    private enum CodingKeys: String, CodingKey {
        case articleChars = "article_chars"
        case costUsd = "cost_usd"
        case error
        case finishedAt = "finished_at"
        case id
        case loginPerformed = "login_performed"
        case startedAt = "started_at"
        case status
        case toolCalls = "tool_calls"
    }
}

/// What the agentic fetch did for this item (motet#102).
///
/// Everything here comes off ``source_items`` and the newest ``enrich_runs`` row; the
/// transcript itself is a separate route, because it is the largest thing on the item and
/// nothing that lists items needs it.
public struct EnrichStepResponse: Codable, Hashable, Sendable {
    public var articleUrl: String?
    public var domain: String?
    public var enrichedAt: Date?
    public var error: String?
    public var originalChars: Int?
    public var run: EnrichRunResponse?
    public var status: String

    public init(
        articleUrl: String? = nil,
        domain: String? = nil,
        enrichedAt: Date? = nil,
        error: String? = nil,
        originalChars: Int? = nil,
        run: EnrichRunResponse? = nil,
        status: String
    ) {
        self.articleUrl = articleUrl
        self.domain = domain
        self.enrichedAt = enrichedAt
        self.error = error
        self.originalChars = originalChars
        self.run = run
        self.status = status
    }

    private enum CodingKeys: String, CodingKey {
        case articleUrl = "article_url"
        case domain
        case enrichedAt = "enriched_at"
        case error
        case originalChars = "original_chars"
        case run
        case status
    }
}

/// One line of a run's **redacted** transcript.
///
/// Redacted on the enrichment service, before it crossed the network: a tool result from
/// anything but the browser is replaced by a note giving its size, and what is kept has had
/// this run's known secrets and the shapes a secret usually takes removed. See
/// ``motet_enrich.redact``.
public struct EnrichTranscriptEntryResponse: Codable, Hashable, Sendable {
    public var args: String?
    public var costUsd: Double?
    public var kind: String
    public var ok: Bool?
    public var result: String?
    public var seq: Int
    public var text: String?
    public var tool: String?

    public init(
        args: String? = nil,
        costUsd: Double? = nil,
        kind: String,
        ok: Bool? = nil,
        result: String? = nil,
        seq: Int,
        text: String? = nil,
        tool: String? = nil
    ) {
        self.args = args
        self.costUsd = costUsd
        self.kind = kind
        self.ok = ok
        self.result = result
        self.seq = seq
        self.text = text
        self.tool = tool
    }

    private enum CodingKeys: String, CodingKey {
        case args
        case costUsd = "cost_usd"
        case kind
        case ok
        case result
        case seq
        case text
        case tool
    }
}

public struct EnrichTranscriptResponse: Codable, Hashable, Sendable {
    public var entries: [EnrichTranscriptEntryResponse]
    public var run: EnrichRunResponse

    public init(entries: [EnrichTranscriptEntryResponse], run: EnrichRunResponse) {
        self.entries = entries
        self.run = run
    }
}

public struct EpisodeResponse: Codable, Hashable, Sendable {
    public var audioBytes: Int?
    public var audioMediaType: String?
    public var createdAt: Date
    public var durationMs: Int
    public var id: String
    public var keepInBacklog: Bool?
    public var lastError: String?
    public var listenedThroughMs: Int
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
        keepInBacklog: Bool? = nil,
        lastError: String? = nil,
        listenedThroughMs: Int,
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
        self.keepInBacklog = keepInBacklog
        self.lastError = lastError
        self.listenedThroughMs = listenedThroughMs
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
        case keepInBacklog = "keep_in_backlog"
        case lastError = "last_error"
        case listenedThroughMs = "listened_through_ms"
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
    public var drainTrigger: Bool
    public var enrichEnabled: Bool?
    public var errorsConfigured: Bool
    public var inferenceMode: String
    public var iosAppLink: Bool
    public var llmOverridesInForce: Bool?
    public var loginConfigured: Bool
    public var mcpOauthConfigured: Bool
    public var mcpTools: Int
    public var revision: String?
    public var service: String
    public var settingsWritable: Bool
    public var status: String
    public var telemetryConfigured: Bool
    public var telemetryExporting: Bool
    public var vaultBackend: String
    public var vaultReady: Bool
    public var voiceConfigured: Bool?

    public init(
        authenticated: Bool,
        drainTrigger: Bool,
        enrichEnabled: Bool? = nil,
        errorsConfigured: Bool,
        inferenceMode: String,
        iosAppLink: Bool,
        llmOverridesInForce: Bool? = nil,
        loginConfigured: Bool,
        mcpOauthConfigured: Bool,
        mcpTools: Int,
        revision: String? = nil,
        service: String,
        settingsWritable: Bool,
        status: String,
        telemetryConfigured: Bool,
        telemetryExporting: Bool,
        vaultBackend: String,
        vaultReady: Bool,
        voiceConfigured: Bool? = nil
    ) {
        self.authenticated = authenticated
        self.drainTrigger = drainTrigger
        self.enrichEnabled = enrichEnabled
        self.errorsConfigured = errorsConfigured
        self.inferenceMode = inferenceMode
        self.iosAppLink = iosAppLink
        self.llmOverridesInForce = llmOverridesInForce
        self.loginConfigured = loginConfigured
        self.mcpOauthConfigured = mcpOauthConfigured
        self.mcpTools = mcpTools
        self.revision = revision
        self.service = service
        self.settingsWritable = settingsWritable
        self.status = status
        self.telemetryConfigured = telemetryConfigured
        self.telemetryExporting = telemetryExporting
        self.vaultBackend = vaultBackend
        self.vaultReady = vaultReady
        self.voiceConfigured = voiceConfigured
    }

    private enum CodingKeys: String, CodingKey {
        case authenticated
        case drainTrigger = "drain_trigger"
        case enrichEnabled = "enrich_enabled"
        case errorsConfigured = "errors_configured"
        case inferenceMode = "inference_mode"
        case iosAppLink = "ios_app_link"
        case llmOverridesInForce = "llm_overrides_in_force"
        case loginConfigured = "login_configured"
        case mcpOauthConfigured = "mcp_oauth_configured"
        case mcpTools = "mcp_tools"
        case revision
        case service
        case settingsWritable = "settings_writable"
        case status
        case telemetryConfigured = "telemetry_configured"
        case telemetryExporting = "telemetry_exporting"
        case vaultBackend = "vault_backend"
        case vaultReady = "vault_ready"
        case voiceConfigured = "voice_configured"
    }
}

/// A source item that is extracted and waiting for the owner to say "ingest now".
///
/// A connected source polls, fetches and extracts on its own — the deterministic, free
/// half — and stops before ``integrate``, the first stage that spends inference. Held is
/// ``state = 'pending'`` with no integrate job, and this is the list of those. A paste
/// never lingers here: it queues its job on arrival.
public struct HeldSourceItemResponse: Codable, Hashable, Sendable {
    public var chars: Int
    public var id: String
    public var preview: String
    public var receivedAt: Date
    public var sourceId: String
    public var sourceKind: String
    public var sourceName: String
    public var title: String

    public init(
        chars: Int,
        id: String,
        preview: String,
        receivedAt: Date,
        sourceId: String,
        sourceKind: String,
        sourceName: String,
        title: String
    ) {
        self.chars = chars
        self.id = id
        self.preview = preview
        self.receivedAt = receivedAt
        self.sourceId = sourceId
        self.sourceKind = sourceKind
        self.sourceName = sourceName
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case chars
        case id
        case preview
        case receivedAt = "received_at"
        case sourceId = "source_id"
        case sourceKind = "source_kind"
        case sourceName = "source_name"
        case title
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

/// One ingested item that has not settled into the backlog yet — and why not.
///
/// This exists because "pending" used to be a thing the system knew and never said. A
/// paste was accepted, queued, retried, and eventually abandoned entirely inside the
/// worker, and the only surface that could have shown any of it — the backlog — lists
/// news items, which is precisely what a failed item never becomes.
///
/// ``attempts`` and ``next_attempt_at`` are here so that *retrying* and *stuck* are
/// distinguishable. They are not the same thing to a person standing there waiting, and
/// a spinner that means both is a spinner that means neither.
///
/// **Not always a source item.** A mailbox message whose fetch failed has no
/// ``source_items`` row — that is written when extraction succeeds — so it is reported
/// from its extract job, under a synthesized ``id`` and a ``title`` naming the provider's
/// message id. Every other field means the same thing either way. Ids are
/// opaque to clients and nothing addresses this route's rows, so the two shapes are one
/// response model rather than two (motet#35).
///
/// **``last_error`` is the exception the stage raised, unedited, and that is the decision
/// rather than an oversight.** It is a new egress: an httpx error names the base URL it
/// dialled, a psycopg one names the database host. The caller is the deployment's single
/// owner behind ``require_caller`` — the same person who reads the obs stack, where the
/// identical string already goes — so there is no reader here who could not already see
/// it. Mapping unknown exceptions to a generic string would buy nothing from that reader
/// and would hand them back the "Failed", with no reason, that this whole surface exists
/// to replace. Revisit it when there is more than one account (Phase 3): at that point the
/// reader and the operator stop being the same person, and this becomes a real leak.
public struct IngestionItemResponse: Codable, Hashable, Sendable {
    public var attempts: Int
    public var createdAt: Date
    public var id: String
    public var lastError: String?
    public var maxAttempts: Int
    public var nextAttemptAt: Date?
    public var sourceId: String
    public var sourceKind: String
    public var state: String
    public var title: String

    public init(
        attempts: Int,
        createdAt: Date,
        id: String,
        lastError: String? = nil,
        maxAttempts: Int,
        nextAttemptAt: Date? = nil,
        sourceId: String,
        sourceKind: String,
        state: String,
        title: String
    ) {
        self.attempts = attempts
        self.createdAt = createdAt
        self.id = id
        self.lastError = lastError
        self.maxAttempts = maxAttempts
        self.nextAttemptAt = nextAttemptAt
        self.sourceId = sourceId
        self.sourceKind = sourceKind
        self.state = state
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case attempts
        case createdAt = "created_at"
        case id
        case lastError = "last_error"
        case maxAttempts = "max_attempts"
        case nextAttemptAt = "next_attempt_at"
        case sourceId = "source_id"
        case sourceKind = "source_kind"
        case state
        case title
    }
}

public struct IntegrateResponse: Codable, Hashable, Sendable {
    public var queued: Int
    public var skipped: Int

    public init(queued: Int, skipped: Int) {
        self.queued = queued
        self.skipped = skipped
    }
}

/// Set, change, or clear a mailbox's label-sync settings.
///
/// Both empty turns label sync off. Setting a label does **not** widen the grant: a mailbox
/// connected read-only reports ``needs_reauthorization`` until its owner re-consents.
public struct LabelSyncRequest: Codable, Hashable, Sendable {
    public var addLabel: String?
    public var removeLabel: String?

    public init(addLabel: String? = nil, removeLabel: String? = nil) {
        self.addLabel = addLabel
        self.removeLabel = removeLabel
    }

    private enum CodingKeys: String, CodingKey {
        case addLabel = "add_label"
        case removeLabel = "remove_label"
    }
}

/// One mailbox's label-sync settings, whether they can act, and what they have done.
///
/// ``status`` is the one field a screen branches on. ``off`` — no labels set, and nothing is
/// ever written. ``needs_reauthorization`` — labels are set, but the mailbox was connected
/// read-only, so nothing is written until its owner re-consents; the write-back records
/// that on every deliberate ingest rather than calling Gmail. ``on`` — labels are set and
/// the grant carries ``gmail.modify``.
public struct LabelSyncResponse: Codable, Hashable, Sendable {
    public var addLabel: String?
    public var availableLabels: [String]
    public var failedItems: Int
    public var labelsReadAt: Date?
    public var lastError: String?
    public var lastSyncedAt: Date?
    public var modifyGranted: Bool
    public var removeLabel: String?
    public var status: String

    public init(
        addLabel: String? = nil,
        availableLabels: [String],
        failedItems: Int,
        labelsReadAt: Date? = nil,
        lastError: String? = nil,
        lastSyncedAt: Date? = nil,
        modifyGranted: Bool,
        removeLabel: String? = nil,
        status: String
    ) {
        self.addLabel = addLabel
        self.availableLabels = availableLabels
        self.failedItems = failedItems
        self.labelsReadAt = labelsReadAt
        self.lastError = lastError
        self.lastSyncedAt = lastSyncedAt
        self.modifyGranted = modifyGranted
        self.removeLabel = removeLabel
        self.status = status
    }

    private enum CodingKeys: String, CodingKey {
        case addLabel = "add_label"
        case availableLabels = "available_labels"
        case failedItems = "failed_items"
        case labelsReadAt = "labels_read_at"
        case lastError = "last_error"
        case lastSyncedAt = "last_synced_at"
        case modifyGranted = "modify_granted"
        case removeLabel = "remove_label"
        case status
    }
}

/// How far into an episode the listener has got.
///
/// Invariant 4: we own playback position, so this is a *report* from a client that we
/// record, never a value read back out of a vendor SDK. Invariant 5 is what it does: a
/// story whose segment has been passed is marked read, which is the same fact the backlog
/// screen's toggle writes.
///
/// The body of both ``PUT /v1/episodes/{id}/position`` and
/// ``POST /v1/episodes/{id}/progress``, because they are one write. The name is the
/// server's: ``spoken_through_ms`` is the voice session contract's word for a position
/// that moves backwards when a listener seeks back, and this value deliberately does not.
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

/// Every LLM stage's model and effort, where each came from, and what may be chosen.
public struct LlmConfigResponse: Codable, Hashable, Sendable {
    public var applies: String
    public var models: [LlmModelOption]
    public var precedence: [String]
    public var settingsError: String?
    public var stages: [LlmStageConfigResponse]
    public var writable: Bool
    public var writableEnv: String

    public init(
        applies: String,
        models: [LlmModelOption],
        precedence: [String],
        settingsError: String? = nil,
        stages: [LlmStageConfigResponse],
        writable: Bool,
        writableEnv: String
    ) {
        self.applies = applies
        self.models = models
        self.precedence = precedence
        self.settingsError = settingsError
        self.stages = stages
        self.writable = writable
        self.writableEnv = writableEnv
    }

    private enum CodingKeys: String, CodingKey {
        case applies
        case models
        case precedence
        case settingsError = "settings_error"
        case stages
        case writable
        case writableEnv = "writable_env"
    }
}

/// One catalogue row, as the admin screen's dropdown needs it.
public struct LlmModelOption: Codable, Hashable, Sendable {
    public var adaptiveThinking: Bool
    public var cacheReadUsdPerMtok: Double
    public var cacheWrite1hUsdPerMtok: Double
    public var cacheWriteUsdPerMtok: Double
    public var efforts: [String]
    public var inputUsdPerMtok: Double
    public var outputUsdPerMtok: Double
    public var reasoningOnByDefault: Bool
    public var slug: String

    public init(
        adaptiveThinking: Bool,
        cacheReadUsdPerMtok: Double,
        cacheWrite1hUsdPerMtok: Double,
        cacheWriteUsdPerMtok: Double,
        efforts: [String],
        inputUsdPerMtok: Double,
        outputUsdPerMtok: Double,
        reasoningOnByDefault: Bool,
        slug: String
    ) {
        self.adaptiveThinking = adaptiveThinking
        self.cacheReadUsdPerMtok = cacheReadUsdPerMtok
        self.cacheWrite1hUsdPerMtok = cacheWrite1hUsdPerMtok
        self.cacheWriteUsdPerMtok = cacheWriteUsdPerMtok
        self.efforts = efforts
        self.inputUsdPerMtok = inputUsdPerMtok
        self.outputUsdPerMtok = outputUsdPerMtok
        self.reasoningOnByDefault = reasoningOnByDefault
        self.slug = slug
    }

    private enum CodingKeys: String, CodingKey {
        case adaptiveThinking = "adaptive_thinking"
        case cacheReadUsdPerMtok = "cache_read_usd_per_mtok"
        case cacheWrite1hUsdPerMtok = "cache_write_1h_usd_per_mtok"
        case cacheWriteUsdPerMtok = "cache_write_usd_per_mtok"
        case efforts
        case inputUsdPerMtok = "input_usd_per_mtok"
        case outputUsdPerMtok = "output_usd_per_mtok"
        case reasoningOnByDefault = "reasoning_on_by_default"
        case slug
    }
}

/// Summed completions and tokens for one bucket, and what they cost in USD.
public struct LlmSpend: Codable, Hashable, Sendable {
    public var cacheReadTokens: Int
    public var cacheWriteTokens: Int
    public var completions: Int
    public var inputTokens: Int
    public var outputTokens: Int
    public var reasoningTokens: Int
    public var unpricedCompletions: Int
    public var usd: Double

    public init(
        cacheReadTokens: Int,
        cacheWriteTokens: Int,
        completions: Int,
        inputTokens: Int,
        outputTokens: Int,
        reasoningTokens: Int,
        unpricedCompletions: Int,
        usd: Double
    ) {
        self.cacheReadTokens = cacheReadTokens
        self.cacheWriteTokens = cacheWriteTokens
        self.completions = completions
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.reasoningTokens = reasoningTokens
        self.unpricedCompletions = unpricedCompletions
        self.usd = usd
    }

    private enum CodingKeys: String, CodingKey {
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case completions
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case reasoningTokens = "reasoning_tokens"
        case unpricedCompletions = "unpriced_completions"
        case usd
    }
}

/// One period of the ledger, folded three ways.
public struct LlmSpendBreakdown: Codable, Hashable, Sendable {
    public var queues: JSONValue
    public var stages: JSONValue
    public var users: [AdminUserSpend]

    public init(queues: JSONValue, stages: JSONValue, users: [AdminUserSpend]) {
        self.queues = queues
        self.stages = stages
        self.users = users
    }
}

/// One stage's resolved model and effort, with the whole precedence chain beside it.
///
/// `model`/`effort` are what a job would resolve right now; the `*_source` fields say
/// which rung won. The rungs themselves are reported so the screen can show the chain and
/// not only its answer: `setting_*` is the `settings` row, `stage_env_*` the
/// `MOTET_LLM_*_<STAGE>` variable, `global_env_*` the `MOTET_LLM_*` variable, `default_*`
/// the committed default. Effort values are an effort name or `"off"`.
public struct LlmStageConfigResponse: Codable, Hashable, Sendable {
    public var defaultEffort: String
    public var defaultModel: String
    public var effort: String
    public var effortSource: String
    public var globalEnvEffort: String?
    public var globalEnvModel: String?
    public var model: String
    public var modelSource: String
    public var models: [String]
    public var settingEffort: String?
    public var settingModel: String?
    public var stage: String
    public var stageEnvEffort: String?
    public var stageEnvModel: String?

    public init(
        defaultEffort: String,
        defaultModel: String,
        effort: String,
        effortSource: String,
        globalEnvEffort: String? = nil,
        globalEnvModel: String? = nil,
        model: String,
        modelSource: String,
        models: [String],
        settingEffort: String? = nil,
        settingModel: String? = nil,
        stage: String,
        stageEnvEffort: String? = nil,
        stageEnvModel: String? = nil
    ) {
        self.defaultEffort = defaultEffort
        self.defaultModel = defaultModel
        self.effort = effort
        self.effortSource = effortSource
        self.globalEnvEffort = globalEnvEffort
        self.globalEnvModel = globalEnvModel
        self.model = model
        self.modelSource = modelSource
        self.models = models
        self.settingEffort = settingEffort
        self.settingModel = settingModel
        self.stage = stage
        self.stageEnvEffort = stageEnvEffort
        self.stageEnvModel = stageEnvModel
    }

    private enum CodingKeys: String, CodingKey {
        case defaultEffort = "default_effort"
        case defaultModel = "default_model"
        case effort
        case effortSource = "effort_source"
        case globalEnvEffort = "global_env_effort"
        case globalEnvModel = "global_env_model"
        case model
        case modelSource = "model_source"
        case models
        case settingEffort = "setting_effort"
        case settingModel = "setting_model"
        case stage
        case stageEnvEffort = "stage_env_effort"
        case stageEnvModel = "stage_env_model"
    }
}

/// Set or clear one stage's `settings` rows.
///
/// A field left out is untouched; `null` clears that row; a string sets it. Effort takes
/// an effort name or `"off"`. Only catalogue slugs are accepted, even where the
/// environment allows an unlisted one.
public struct LlmStageConfigUpdate: Codable, Hashable, Sendable {
    public var effort: String?
    public var model: String?

    public init(effort: String? = nil, model: String? = nil) {
        self.effort = effort
        self.model = model
    }
}

/// A session and the token that presents it — or, for a sign-in the iOS app started,
/// the link that hands it back to the app.
///
/// ``token`` is returned exactly once, here — the API stores only its hash, so it cannot
/// be read back. A client that loses it signs in again.
///
/// Exactly one of ``token`` and ``handoff_url`` is set. A browser sign-in gets a token.
/// A sign-in started by ``POST /v1/auth/native/start`` finishes in the app's in-app
/// browser, which must not keep the session: it gets ``handoff_url`` instead, navigates
/// to it, and the app redeems the code inside it at ``POST /v1/auth/native/redeem``.
public struct LoginResponse: Codable, Hashable, Sendable {
    public var email: String
    public var expiresAt: Date?
    public var handoffUrl: String?
    public var token: String?

    public init(
        email: String,
        expiresAt: Date? = nil,
        handoffUrl: String? = nil,
        token: String? = nil
    ) {
        self.email = email
        self.expiresAt = expiresAt
        self.handoffUrl = handoffUrl
        self.token = token
    }

    private enum CodingKeys: String, CodingKey {
        case email
        case expiresAt = "expires_at"
        case handoffUrl = "handoff_url"
        case token
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

/// An MCP client's authorization, verified by Google sign-in and waiting for a person's yes.
///
/// motet#111. The code is already minted and bound to the client, but it reaches the client
/// only if the SPA navigates to ``redirect_url``, which it does after the person has seen
/// ``client_name`` and ``redirect_host`` and pressed Allow. ``deny_url`` tells the client no.
public struct McpAuthorizationResponse: Codable, Hashable, Sendable {
    public var clientName: String
    public var denyUrl: String
    public var email: String
    public var redirectHost: String
    public var redirectUrl: String

    public init(
        clientName: String,
        denyUrl: String,
        email: String,
        redirectHost: String,
        redirectUrl: String
    ) {
        self.clientName = clientName
        self.denyUrl = denyUrl
        self.email = email
        self.redirectHost = redirectHost
        self.redirectUrl = redirectUrl
    }

    private enum CodingKeys: String, CodingKey {
        case clientName = "client_name"
        case denyUrl = "deny_url"
        case email
        case redirectHost = "redirect_host"
        case redirectUrl = "redirect_url"
    }
}

/// A deduped story. Read state lives here, per invariant 5 — not per episode.
public struct NewsItemResponse: Codable, Hashable, Sendable {
    public var createdAt: Date
    public var id: String
    public var read: Bool
    public var sourceItemIds: [String]
    public var sources: [NewsItemSourceRef]
    public var summary: String
    public var title: String

    public init(
        createdAt: Date,
        id: String,
        read: Bool,
        sourceItemIds: [String],
        sources: [NewsItemSourceRef],
        summary: String,
        title: String
    ) {
        self.createdAt = createdAt
        self.id = id
        self.read = read
        self.sourceItemIds = sourceItemIds
        self.sources = sources
        self.summary = summary
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case createdAt = "created_at"
        case id
        case read
        case sourceItemIds = "source_item_ids"
        case sources
        case summary
        case title
    }
}

/// A source item a news item is backed by, named so a list can show it.
public struct NewsItemSourceRef: Codable, Hashable, Sendable {
    public var id: String
    public var title: String

    public init(id: String, title: String) {
        self.id = id
        self.title = title
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

/// Whether anything is actually draining the queues — motet#38's missing fact.
///
/// The Processing panel used to tell the user "a worker takes it off the queue within a
/// few seconds" whatever was true, because nothing in the system could say otherwise: a
/// queued item looks the same whether a worker is chewing through a backlog or whether no
/// worker has run since Tuesday. The client cannot derive it either — an item's age says
/// how long it has waited, not whether anything is coming.
///
/// ``worker_last_seen_at`` is null when no worker has *ever* run against this database,
/// which is a different statement from "one ran a while ago" and reads differently on
/// screen. Per-queue rows are here for the operator's version of the same question; the
/// SPA reads the aggregate, because a Phase 1 deployment runs one process over all of
/// them (``runner all``).
///
/// ``now`` is here so the client never has to compare a database timestamp against a
/// browser clock. It is a small field against a whole class of wrongness: a laptop
/// resumed from sleep, or an unsynced VM, would otherwise report a perfectly healthy
/// worker as gone and put a red banner over a pipeline that is running fine.
public struct ProcessingStatusResponse: Codable, Hashable, Sendable {
    public var now: Date
    public var queues: [QueueHeartbeatResponse]
    public var readiness: [QueueReadinessResponse]
    public var workerLastSeenAt: Date?

    public init(
        now: Date,
        queues: [QueueHeartbeatResponse],
        readiness: [QueueReadinessResponse],
        workerLastSeenAt: Date? = nil
    ) {
        self.now = now
        self.queues = queues
        self.readiness = readiness
        self.workerLastSeenAt = workerLastSeenAt
    }

    private enum CodingKeys: String, CodingKey {
        case now
        case queues
        case readiness
        case workerLastSeenAt = "worker_last_seen_at"
    }
}

/// One step of stage 2. Dedup is the only step today; enrichment steps will join it.
///
/// ``status`` follows the step's job: ``queued`` (first attempt due, or a retry backing
/// off), ``running``, ``done`` or ``failed``. ``cost_recorded`` is false: the step's spend
/// is logged beside the source item id and metered per stage, never stored per item.
public struct ProcessingStepResponse: Codable, Hashable, Sendable {
    public var costRecorded: Bool
    public var decision: DedupDecisionResponse?
    public var enrich: EnrichStepResponse?
    public var error: String?
    public var finishedAt: Date?
    public var job: SourceItemJobResponse?
    public var outcome: String?
    public var status: String
    public var step: String

    public init(
        costRecorded: Bool,
        decision: DedupDecisionResponse? = nil,
        enrich: EnrichStepResponse? = nil,
        error: String? = nil,
        finishedAt: Date? = nil,
        job: SourceItemJobResponse? = nil,
        outcome: String? = nil,
        status: String,
        step: String
    ) {
        self.costRecorded = costRecorded
        self.decision = decision
        self.enrich = enrich
        self.error = error
        self.finishedAt = finishedAt
        self.job = job
        self.outcome = outcome
        self.status = status
        self.step = step
    }

    private enum CodingKeys: String, CodingKey {
        case costRecorded = "cost_recorded"
        case decision
        case enrich
        case error
        case finishedAt = "finished_at"
        case job
        case outcome
        case status
        case step
    }
}

/// One queue, and when a worker was last draining it.
public struct QueueHeartbeatResponse: Codable, Hashable, Sendable {
    public var lastSeenAt: Date
    public var queue: String

    public init(lastSeenAt: Date, queue: String) {
        self.lastSeenAt = lastSeenAt
        self.queue = queue
    }

    private enum CodingKeys: String, CodingKey {
        case lastSeenAt = "last_seen_at"
        case queue
    }
}

/// One queue's scaling signal: what is due, and how many workers could take it.
///
/// Separate from :class:`QueueHeartbeatResponse` rather than folded into it, because the
/// two lists answer different questions over different sets. A heartbeat exists only for a
/// queue a worker has *run*; readiness exists for every queue, and the case it has to
/// cover is precisely the one with no worker — a queue scaled to zero emits no gauge, so
/// this route is the only place its backlog is visible (motet#78). Merging them would
/// have meant widening ``last_seen_at`` to nullable, which is a breaking change to a
/// shipped field for no gain.
public struct QueueReadinessResponse: Codable, Hashable, Sendable {
    public var blockedKeys: Int
    public var queue: String
    public var ready: Int
    public var readyKeys: Int

    public init(blockedKeys: Int, queue: String, ready: Int, readyKeys: Int) {
        self.blockedKeys = blockedKeys
        self.queue = queue
        self.ready = ready
        self.readyKeys = readyKeys
    }

    private enum CodingKeys: String, CodingKey {
        case blockedKeys = "blocked_keys"
        case queue
        case ready
        case readyKeys = "ready_keys"
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

/// Ask the owner to grant a connected mailbox the scope label sync needs.
public struct ReauthorizeSourceRequest: Codable, Hashable, Sendable {
    public var redirectUri: String

    public init(redirectUri: String) {
        self.redirectUri = redirectUri
    }

    private enum CodingKeys: String, CodingKey {
        case redirectUri = "redirect_uri"
    }
}

/// The code the handoff link carried, and the verifier only the app holds.
public struct RedeemNativeLoginRequest: Codable, Hashable, Sendable {
    public var code: String
    public var codeVerifier: String

    public init(code: String, codeVerifier: String) {
        self.code = code
        self.codeVerifier = codeVerifier
    }

    private enum CodingKeys: String, CodingKey {
        case code
        case codeVerifier = "code_verifier"
    }
}

/// How many sessions a revoke-everywhere took out.
public struct RevokedResponse: Codable, Hashable, Sendable {
    public var revoked: Int

    public init(revoked: Int) {
        self.revoked = revoked
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

/// Who the caller is, as far as this API is concerned.
///
/// Answers for the shared API token too, which is what lets the SPA show "signed in as
/// …" or "using an API token" without guessing from what it has in storage.
public struct SessionResponse: Codable, Hashable, Sendable {
    public var admin: Bool
    public var email: String?
    public var expiresAt: Date?
    public var how: String
    public var loginConfigured: Bool

    public init(
        admin: Bool,
        email: String? = nil,
        expiresAt: Date? = nil,
        how: String,
        loginConfigured: Bool
    ) {
        self.admin = admin
        self.email = email
        self.expiresAt = expiresAt
        self.how = how
        self.loginConfigured = loginConfigured
    }

    private enum CodingKeys: String, CodingKey {
        case admin
        case email
        case expiresAt = "expires_at"
        case how
        case loginConfigured = "login_configured"
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

/// One source item across its three stages: pulled in, processed, news item.
///
/// A read over ``source_items``, the newest ``integrate`` job and ``news_item_sources``.
/// ``processed`` is a list so that enrichment steps can join dedup without a new shape;
/// it is empty while the item is held or once it is dismissed, and ``news_items`` is
/// empty until dedup has run.
public struct SourceItemDetailResponse: Codable, Hashable, Sendable {
    public var id: String
    public var newsItems: [SourceItemNewsItemResponse]
    public var processed: [ProcessingStepResponse]
    public var pulled: SourceItemPulledStage
    public var state: String
    public var status: String
    public var title: String

    public init(
        id: String,
        newsItems: [SourceItemNewsItemResponse],
        processed: [ProcessingStepResponse],
        pulled: SourceItemPulledStage,
        state: String,
        status: String,
        title: String
    ) {
        self.id = id
        self.newsItems = newsItems
        self.processed = processed
        self.pulled = pulled
        self.state = state
        self.status = status
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case newsItems = "news_items"
        case processed
        case pulled
        case state
        case status
        case title
    }
}

/// Which held source items to act on. At most as many as the held list returns.
public struct SourceItemIdsRequest: Codable, Hashable, Sendable {
    public var ids: [String]

    public init(ids: [String]) {
        self.ids = ids
    }
}

/// The newest ``integrate`` job for a source item, as the queue holds it.
public struct SourceItemJobResponse: Codable, Hashable, Sendable {
    public var attempts: Int
    public var createdAt: Date
    public var id: Int
    public var lastError: String?
    public var lockedAt: Date?
    public var maxAttempts: Int
    public var runAt: Date
    public var state: String
    public var updatedAt: Date
    public var workCommitted: Bool

    public init(
        attempts: Int,
        createdAt: Date,
        id: Int,
        lastError: String? = nil,
        lockedAt: Date? = nil,
        maxAttempts: Int,
        runAt: Date,
        state: String,
        updatedAt: Date,
        workCommitted: Bool
    ) {
        self.attempts = attempts
        self.createdAt = createdAt
        self.id = id
        self.lastError = lastError
        self.lockedAt = lockedAt
        self.maxAttempts = maxAttempts
        self.runAt = runAt
        self.state = state
        self.updatedAt = updatedAt
        self.workCommitted = workCommitted
    }

    private enum CodingKeys: String, CodingKey {
        case attempts
        case createdAt = "created_at"
        case id
        case lastError = "last_error"
        case lockedAt = "locked_at"
        case maxAttempts = "max_attempts"
        case runAt = "run_at"
        case state
        case updatedAt = "updated_at"
        case workCommitted = "work_committed"
    }
}

/// Stage 3: the deduped news item this source item feeds.
public struct SourceItemNewsItemResponse: Codable, Hashable, Sendable {
    public var id: String
    public var position: Int
    public var read: Bool
    public var sourceCount: Int
    public var summary: String
    public var title: String

    public init(
        id: String,
        position: Int,
        read: Bool,
        sourceCount: Int,
        summary: String,
        title: String
    ) {
        self.id = id
        self.position = position
        self.read = read
        self.sourceCount = sourceCount
        self.summary = summary
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case position
        case read
        case sourceCount = "source_count"
        case summary
        case title
    }
}

/// Stage 1 of a source item's life: what the deterministic scrape pulled in.
///
/// Everything here was produced without a model — a poll, a fetch, and
/// ``motet_sources.extract``. ``text`` is the extracted text, not the raw message: the
/// RFC 822 bytes are never stored, which ``raw_stored`` says out loud so that the UI does
/// not call the extracted text "the email".
public struct SourceItemPulledStage: Codable, Hashable, Sendable {
    public var chars: Int
    public var externalId: String?
    public var rawStored: Bool
    public var receivedAt: Date
    public var sourceId: String
    public var sourceKind: String
    public var sourceName: String
    public var storedAt: Date
    public var text: String

    public init(
        chars: Int,
        externalId: String? = nil,
        rawStored: Bool,
        receivedAt: Date,
        sourceId: String,
        sourceKind: String,
        sourceName: String,
        storedAt: Date,
        text: String
    ) {
        self.chars = chars
        self.externalId = externalId
        self.rawStored = rawStored
        self.receivedAt = receivedAt
        self.sourceId = sourceId
        self.sourceKind = sourceKind
        self.sourceName = sourceName
        self.storedAt = storedAt
        self.text = text
    }

    private enum CodingKeys: String, CodingKey {
        case chars
        case externalId = "external_id"
        case rawStored = "raw_stored"
        case receivedAt = "received_at"
        case sourceId = "source_id"
        case sourceKind = "source_kind"
        case sourceName = "source_name"
        case storedAt = "stored_at"
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

/// A place source items come from — pasted text, or a connected mailbox.
///
/// **Two rows with no credential mean different things, and ``disconnected_at`` is what
/// tells them apart.** ``POST /v1/sources/connect`` creates a row before the user leaves
/// for the provider, so a consent that was cancelled leaves one behind that never held a
/// credential. A mailbox that was disconnected held one and gave it back. Both are
/// ``connected: false, active: false``. A row disconnected before ``disconnected_at``
/// existed has it null, and its ``last_polled_at`` is the only tell.
public struct SourceResponse: Codable, Hashable, Sendable {
    public var active: Bool
    public var connected: Bool
    public var createdAt: Date
    public var disconnectedAt: Date?
    public var firstSyncDays: Int?
    public var id: String
    public var itemsIntegrated: Int
    public var itemsPulledIn: Int
    public var kind: String
    public var labelSync: LabelSyncResponse?
    public var lastError: String?
    public var lastPolledAt: Date?
    public var lastSync: SourceSyncResult?
    public var name: String
    public var query: String?
    public var scopes: [String]

    public init(
        active: Bool,
        connected: Bool,
        createdAt: Date,
        disconnectedAt: Date? = nil,
        firstSyncDays: Int? = nil,
        id: String,
        itemsIntegrated: Int,
        itemsPulledIn: Int,
        kind: String,
        labelSync: LabelSyncResponse? = nil,
        lastError: String? = nil,
        lastPolledAt: Date? = nil,
        lastSync: SourceSyncResult? = nil,
        name: String,
        query: String? = nil,
        scopes: [String]
    ) {
        self.active = active
        self.connected = connected
        self.createdAt = createdAt
        self.disconnectedAt = disconnectedAt
        self.firstSyncDays = firstSyncDays
        self.id = id
        self.itemsIntegrated = itemsIntegrated
        self.itemsPulledIn = itemsPulledIn
        self.kind = kind
        self.labelSync = labelSync
        self.lastError = lastError
        self.lastPolledAt = lastPolledAt
        self.lastSync = lastSync
        self.name = name
        self.query = query
        self.scopes = scopes
    }

    private enum CodingKeys: String, CodingKey {
        case active
        case connected
        case createdAt = "created_at"
        case disconnectedAt = "disconnected_at"
        case firstSyncDays = "first_sync_days"
        case id
        case itemsIntegrated = "items_integrated"
        case itemsPulledIn = "items_pulled_in"
        case kind
        case labelSync = "label_sync"
        case lastError = "last_error"
        case lastPolledAt = "last_polled_at"
        case lastSync = "last_sync"
        case name
        case query
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

/// What the most recent poll of a connected source found — or why it gave up.
public struct SourceSyncResult: Codable, Hashable, Sendable {
    public var at: Date
    public var caughtUp: Bool
    public var error: String?
    public var queued: Int
    public var seen: Int

    public init(at: Date, caughtUp: Bool, error: String? = nil, queued: Int, seen: Int) {
        self.at = at
        self.caughtUp = caughtUp
        self.error = error
        self.queued = queued
        self.seen = seen
    }

    private enum CodingKeys: String, CodingKey {
        case at
        case caughtUp = "caught_up"
        case error
        case queued
        case seen
    }
}

/// Begin a Google sign-in. Answered with a URL for the browser to visit.
public struct StartLoginRequest: Codable, Hashable, Sendable {
    public var redirectUri: String

    public init(redirectUri: String) {
        self.redirectUri = redirectUri
    }

    private enum CodingKeys: String, CodingKey {
        case redirectUri = "redirect_uri"
    }
}

/// Where to send the browser, and the state that identifies this sign-in.
public struct StartLoginResponse: Codable, Hashable, Sendable {
    public var authorizationUrl: String
    public var state: String

    public init(authorizationUrl: String, state: String) {
        self.authorizationUrl = authorizationUrl
        self.state = state
    }

    private enum CodingKeys: String, CodingKey {
        case authorizationUrl = "authorization_url"
        case state
    }
}

/// Begin a Google sign-in on behalf of the iOS app.
public struct StartNativeLoginRequest: Codable, Hashable, Sendable {
    public var appLinkDomain: String?
    public var codeChallenge: String

    public init(appLinkDomain: String? = nil, codeChallenge: String) {
        self.appLinkDomain = appLinkDomain
        self.codeChallenge = codeChallenge
    }

    private enum CodingKeys: String, CodingKey {
        case appLinkDomain = "app_link_domain"
        case codeChallenge = "code_challenge"
    }
}

/// Where the app's in-app browser should go.
public struct StartNativeLoginResponse: Codable, Hashable, Sendable {
    public var authorizationUrl: String
    public var callbackHost: String?
    public var callbackPath: String?
    public var callbackScheme: String

    public init(
        authorizationUrl: String,
        callbackHost: String? = nil,
        callbackPath: String? = nil,
        callbackScheme: String
    ) {
        self.authorizationUrl = authorizationUrl
        self.callbackHost = callbackHost
        self.callbackPath = callbackPath
        self.callbackScheme = callbackScheme
    }

    private enum CodingKeys: String, CodingKey {
        case authorizationUrl = "authorization_url"
        case callbackHost = "callback_host"
        case callbackPath = "callback_path"
        case callbackScheme = "callback_scheme"
    }
}

public struct StartVoiceSessionRequest: Codable, Hashable, Sendable {
    public var spokenThroughMs: Int?

    public init(spokenThroughMs: Int? = nil) {
        self.spokenThroughMs = spokenThroughMs
    }

    private enum CodingKeys: String, CodingKey {
        case spokenThroughMs = "spoken_through_ms"
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

/// What a client needs to open the voice socket, and nothing about the vendor behind it.
public struct VoiceSessionResponse: Codable, Hashable, Sendable {
    public var arm: String
    public var authenticateFrame: JSONValue
    public var conversational: Bool
    public var expiresAt: String
    public var sessionId: String
    public var sessionToken: String
    public var websocketUrl: String

    public init(
        arm: String,
        authenticateFrame: JSONValue,
        conversational: Bool,
        expiresAt: String,
        sessionId: String,
        sessionToken: String,
        websocketUrl: String
    ) {
        self.arm = arm
        self.authenticateFrame = authenticateFrame
        self.conversational = conversational
        self.expiresAt = expiresAt
        self.sessionId = sessionId
        self.sessionToken = sessionToken
        self.websocketUrl = websocketUrl
    }

    private enum CodingKeys: String, CodingKey {
        case arm
        case authenticateFrame = "authenticate_frame"
        case conversational
        case expiresAt = "expires_at"
        case sessionId = "session_id"
        case sessionToken = "session_token"
        case websocketUrl = "websocket_url"
    }
}

/// Whether Play Live can run here — asked before a button is offered.
public struct VoiceStatusResponse: Codable, Hashable, Sendable {
    public var configured: Bool
    public var reason: String?

    public init(configured: Bool, reason: String? = nil) {
        self.configured = configured
        self.reason = reason
    }
}

/// The landing page's waitlist form was accepted.
///
/// The same answer whether the address is new or already listed, so the route cannot be
/// used to learn who is on the list.
public struct WaitlistJoinResponse: Codable, Hashable, Sendable {
    public var status: String

    public init(status: String) {
        self.status = status
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

    /// `GET /v1/admin/llm-config` — Get Llm Config
    public static var getLlmConfig: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/admin/llm-config")
    }

    /// `PUT /v1/admin/llm-config/{stage}` — Put Llm Config
    public static func putLlmConfig(stage: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "PUT", path: "/v1/admin/llm-config/\(MotetPathComponent(stage))")
    }

    /// `GET /v1/admin/llm-spend` — Get Llm Spend
    public static var getLlmSpend: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/admin/llm-spend")
    }

    /// `GET /v1/admin/overview` — Admin Overview
    public static func adminOverview(userId: String? = nil, before: Int? = nil, limit: Int? = nil) -> HTTPEndpoint {
        var query: [String: String] = [:]
        if let userId { query["user_id"] = String(describing: userId) }
        if let before { query["before"] = String(describing: before) }
        if let limit { query["limit"] = String(describing: limit) }
        return HTTPEndpoint(method: "GET", path: "/v1/admin/overview", query: query)
    }

    /// `GET /v1/admin/waitlist` — Admin Waitlist
    public static func adminWaitlist(before: Int? = nil, limit: Int? = nil) -> HTTPEndpoint {
        var query: [String: String] = [:]
        if let before { query["before"] = String(describing: before) }
        if let limit { query["limit"] = String(describing: limit) }
        return HTTPEndpoint(method: "GET", path: "/v1/admin/waitlist", query: query)
    }

    /// `POST /v1/auth/google/callback` — Complete Login
    public static var completeLogin: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/auth/google/callback")
    }

    /// `POST /v1/auth/google/start` — Start Login
    public static var startLogin: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/auth/google/start")
    }

    /// `POST /v1/auth/logout` — Logout
    public static var logout: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/auth/logout")
    }

    /// `POST /v1/auth/logout-all` — Logout Everywhere
    public static var logoutEverywhere: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/auth/logout-all")
    }

    /// `POST /v1/auth/mcp/callback` — Complete Mcp Authorization
    public static var completeMcpAuthorization: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/auth/mcp/callback")
    }

    /// `POST /v1/auth/native/redeem` — Redeem Native Login
    public static var redeemNativeLogin: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/auth/native/redeem")
    }

    /// `POST /v1/auth/native/start` — Start Native Login
    public static var startNativeLogin: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/auth/native/start")
    }

    /// `GET /v1/auth/session` — Current Session
    public static var currentSession: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/auth/session")
    }

    /// `GET /v1/connectors` — List Connectors
    public static var listConnectors: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/connectors")
    }

    /// `POST /v1/connectors` — Create Connector
    public static var createConnector: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/connectors")
    }

    /// `POST /v1/connectors/oauth/callback` — Connector Oauth Callback
    public static var connectorOauthCallback: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/connectors/oauth/callback")
    }

    /// `DELETE /v1/connectors/{connector_id}` — Delete Connector
    public static func deleteConnector(connectorId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "DELETE", path: "/v1/connectors/\(MotetPathComponent(connectorId))")
    }

    /// `POST /v1/connectors/{connector_id}/authorize` — Authorize Connector
    public static func authorizeConnector(connectorId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/connectors/\(MotetPathComponent(connectorId))/authorize")
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

    /// `PUT /v1/episodes/{episode_id}/position` — Set Playback Position
    public static func setPlaybackPosition(episodeId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "PUT", path: "/v1/episodes/\(MotetPathComponent(episodeId))/position")
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

    /// `POST /v1/episodes/{episode_id}/voice-session` — Start Voice Session
    public static func startVoiceSession(episodeId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/episodes/\(MotetPathComponent(episodeId))/voice-session")
    }

    /// `GET /v1/feed` — Get Feed Info
    public static var getFeedInfo: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/feed")
    }

    /// `GET /v1/feed/artwork.png` — Feed Artwork
    public static var feedArtwork: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/feed/artwork.png")
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

    /// `GET /v1/ingestion` — List Ingestion
    public static var listIngestion: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/ingestion")
    }

    /// `GET /v1/news-items` — List News Items
    public static var listNewsItems: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/news-items")
    }

    /// `POST /v1/news-items/{news_item_id}/read` — Set News Item Read
    public static func setNewsItemRead(newsItemId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/news-items/\(MotetPathComponent(newsItemId))/read")
    }

    /// `GET /v1/processing` — Processing Status
    public static var processingStatus: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/processing")
    }

    /// `POST /v1/source-items/dismiss` — Dismiss Source Items
    public static var dismissSourceItems: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/source-items/dismiss")
    }

    /// `GET /v1/source-items/held` — List Held Source Items
    public static var listHeldSourceItems: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/source-items/held")
    }

    /// `POST /v1/source-items/integrate` — Integrate Source Items
    public static var integrateSourceItems: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/source-items/integrate")
    }

    /// `GET /v1/source-items/{source_item_id}` — Get Source Item Detail
    public static func getSourceItemDetail(sourceItemId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/source-items/\(MotetPathComponent(sourceItemId))")
    }

    /// `GET /v1/source-items/{source_item_id}/enrich-transcript` — Get Enrich Transcript
    public static func getEnrichTranscript(sourceItemId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/source-items/\(MotetPathComponent(sourceItemId))/enrich-transcript")
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

    /// `DELETE /v1/sources/{source_id}` — Remove Source
    public static func removeSource(sourceId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "DELETE", path: "/v1/sources/\(MotetPathComponent(sourceId))")
    }

    /// `DELETE /v1/sources/{source_id}/credentials` — Disconnect Source
    public static func disconnectSource(sourceId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "DELETE", path: "/v1/sources/\(MotetPathComponent(sourceId))/credentials")
    }

    /// `PUT /v1/sources/{source_id}/label-sync` — Set Label Sync
    public static func setLabelSync(sourceId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "PUT", path: "/v1/sources/\(MotetPathComponent(sourceId))/label-sync")
    }

    /// `POST /v1/sources/{source_id}/poll` — Poll Source
    public static func pollSource(sourceId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/sources/\(MotetPathComponent(sourceId))/poll")
    }

    /// `POST /v1/sources/{source_id}/reauthorize` — Reauthorize Source
    public static func reauthorizeSource(sourceId: String) -> HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/sources/\(MotetPathComponent(sourceId))/reauthorize")
    }

    /// `GET /v1/voice` — Voice Status
    public static var voiceStatus: HTTPEndpoint {
        return HTTPEndpoint(method: "GET", path: "/v1/voice")
    }

    /// `POST /v1/waitlist` — Join the waitlist
    public static var joinTheWaitlist: HTTPEndpoint {
        return HTTPEndpoint(method: "POST", path: "/v1/waitlist")
    }
}
