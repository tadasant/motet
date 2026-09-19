import XCTest
@testable import MotetKit

/// The Sources screen's rules — one reading per row, the same one the SPA gives — and the
/// wire the screen speaks.
final class SourceStatusTests: XCTestCase {
    private func source(
        id: String = "src_1",
        kind: String = "gmail",
        active: Bool = true,
        connected: Bool = true,
        lastError: String? = nil,
        lastPolledAt: Date? = nil,
        disconnectedAt: Date? = nil,
        lastSync: SourceSyncResult? = nil
    ) -> SourceResponse {
        SourceResponse(
            active: active, connected: connected, createdAt: Date(timeIntervalSince1970: 0),
            disconnectedAt: disconnectedAt, id: id, itemsIntegrated: 0, itemsPulledIn: 0,
            kind: kind, lastError: lastError, lastPolledAt: lastPolledAt, lastSync: lastSync,
            name: "Mail", scopes: []
        )
    }

    func testAnAbandonedConsentIsNotABrokenSource() {
        // The row `/v1/sources/connect` creates before the person leaves for Google, still
        // without a credential: never polled, never disconnected.
        XCTAssertEqual(SourceStatus.row(source(active: false, connected: false)), .awaitingConsent)
    }

    func testADisconnectedMailboxIsToldApartFromAnAbandonedOne() {
        let now = Date()
        XCTAssertEqual(
            SourceStatus.row(source(active: false, connected: false, disconnectedAt: now)),
            .disconnected
        )
        // Disconnected before the API recorded `disconnected_at`: having polled is the tell.
        XCTAssertEqual(
            SourceStatus.row(source(active: false, connected: false, lastPolledAt: now)),
            .disconnected
        )
    }

    func testAnErrorOutranksAPause() {
        XCTAssertEqual(SourceStatus.row(source(active: false, lastError: "401")), .error)
        XCTAssertEqual(SourceStatus.row(source(active: false)), .paused)
        XCTAssertEqual(SourceStatus.row(source()), .connected)
    }

    func testPasteIsReadOffActiveAlone() {
        XCTAssertEqual(SourceStatus.row(source(kind: "paste", connected: false)), .ready)
        XCTAssertFalse(SourceStatus.isPollable(source(kind: "paste")))
    }

    func testTheCardShowsWhatNeedsAttentionFirst() {
        let working = source(id: "a")
        let cancelled = source(id: "b", active: false, connected: false)
        let broken = source(id: "c", lastError: "revoked")
        XCTAssertEqual(SourceStatus.card(.gmail, rows: [cancelled, working]), .connected(count: 1))
        XCTAssertEqual(SourceStatus.card(.gmail, rows: [working, broken]), .error)
        XCTAssertEqual(SourceStatus.card(.gmail, rows: [cancelled]), .awaitingConsent)
        XCTAssertEqual(SourceStatus.card(.gmail, rows: []), .notConnected)
        XCTAssertEqual(SourceStatus.card(.paste, rows: []), .alwaysOn)
        XCTAssertEqual(SourceStatus.card(.rss, rows: [working]), .comingSoon)
        XCTAssertEqual(SourceStatus.Card.connected(count: 2).label, "2 connected")
    }

    func testRowsAreOrderedErrorFirstAndStableOtherwise() {
        let rows = [
            source(id: "cancelled", active: false, connected: false),
            source(id: "one"),
            source(id: "broken", lastError: "x"),
            source(id: "two"),
        ]
        XCTAssertEqual(SourceStatus.ordered(rows).map(\.id), ["broken", "one", "two", "cancelled"])
    }

    func testIntegrationsJoinRowsByKind() {
        let rows = [source(id: "g"), source(id: "p", kind: "paste")]
        XCTAssertEqual(Integration.gmail.rows(in: rows).map(\.id), ["g"])
        XCTAssertEqual(Integration.paste.rows(in: rows).map(\.id), ["p"])
        XCTAssertTrue(Integration.xBookmarks.rows(in: rows).isEmpty)
    }

    func testCountsArePerSourceNotPerKind() {
        let mine = source(id: "mine")
        let when = Date(timeIntervalSince1970: 0)
        let held = [
            HeldSourceItemResponse(chars: 1, id: "h1", preview: "", receivedAt: when, sourceId: "mine",
                                   sourceKind: "gmail", sourceName: "", title: ""),
            HeldSourceItemResponse(chars: 1, id: "h2", preview: "", receivedAt: when, sourceId: "other",
                                   sourceKind: "gmail", sourceName: "", title: ""),
        ]
        func item(_ state: String, _ sourceId: String = "mine") -> IngestionItemResponse {
            IngestionItemResponse(attempts: 1, createdAt: when, id: UUID().uuidString, maxAttempts: 5,
                                  sourceId: sourceId, sourceKind: "gmail", state: state, title: "")
        }
        let counts = SourceStatus.counts(
            for: mine, held: held,
            ingestion: [item("pending"), item("failed"), item("integrated"), item("integrated"), item("failed", "other")]
        )
        XCTAssertEqual(counts, SourceStatus.Counts(held: 1, processing: 1, failed: 1, integrated: 2))
    }

    func testTheLastSyncIsASentence() {
        let at = Date(timeIntervalSince1970: 0)
        XCTAssertNil(SourceStatus.describeLastSync(source()))
        XCTAssertEqual(
            SourceStatus.describeLastSync(source(lastSync: SourceSyncResult(at: at, caughtUp: true, queued: 0, seen: 0))),
            "No new messages."
        )
        XCTAssertEqual(
            SourceStatus.describeLastSync(source(lastSync: SourceSyncResult(at: at, caughtUp: true, queued: 1, seen: 3))),
            "Looked at 3 messages; 1 was new."
        )
        XCTAssertEqual(
            SourceStatus.describeLastSync(source(lastSync: SourceSyncResult(at: at, caughtUp: false, queued: 0, seen: 1))),
            "Looked at 1 message; none were new. Still catching up: each sync queues the next."
        )
        XCTAssertEqual(
            SourceStatus.describeLastSync(source(lastSync: SourceSyncResult(at: at, caughtUp: true, error: "revoked", queued: 0, seen: 0))),
            "The last sync gave up: revoked"
        )
    }

    func testSyncNowWatchesTheSyncNotTheLastPoll() {
        // An extraction that skips a message moves `last_polled_at` too; only `last_sync.at`
        // is written by a poll, so only it may end the watch.
        let polled = Date(timeIntervalSince1970: 100)
        let row = source(lastPolledAt: polled)
        XCTAssertNil(SourceStatus.syncedAt(row))
        XCTAssertEqual(SourceStatus.lastSyncedAt(row), polled)
    }

    func testAMissingHeartbeatIsNotAnIdleWorker() {
        let now = Date(timeIntervalSince1970: 10_000)
        XCTAssertEqual(SourceStatus.worker(nil), .unknown)
        XCTAssertEqual(
            SourceStatus.worker(ProcessingStatusResponse(now: now, queues: [], readiness: [])), .never
        )
        XCTAssertEqual(
            SourceStatus.worker(ProcessingStatusResponse(
                now: now, queues: [], readiness: [], workerLastSeenAt: now.addingTimeInterval(-60)
            )),
            .running
        )
        XCTAssertEqual(
            SourceStatus.worker(ProcessingStatusResponse(
                now: now, queues: [], readiness: [], workerLastSeenAt: now.addingTimeInterval(-3_600)
            )),
            .idle
        )
    }

    func testALabelMoveIsDescribed() {
        XCTAssertEqual(SourceStatus.describeLabelMove(remove: "Newsletters", add: "Completed"),
                       "moves its message from Newsletters to Completed")
        XCTAssertEqual(SourceStatus.describeLabelMove(remove: "INBOX", add: ""), "takes INBOX off its message")
        XCTAssertNil(SourceStatus.describeLabelMove(remove: nil, add: ""))
    }

    func testScopesReadAsWhatWasGranted() {
        XCTAssertEqual(SourceStatus.describeScope("https://www.googleapis.com/auth/gmail.readonly"), "read-only mail")
        XCTAssertEqual(SourceStatus.describeScope("something.else"), "something.else")
    }
}

final class ConnectorStatusTests: XCTestCase {
    private func connector(
        kind: String, status: String = "ready", username: String? = nil, hasSecret: Bool = false
    ) -> ConnectorResponse {
        let when = Date(timeIntervalSince1970: 0)
        return ConnectorResponse(
            createdAt: when, domain: "example.com", domains: [], hasSecret: hasSecret, id: "c1",
            kind: kind, label: "Example", oauthRegistered: false, status: status, updatedAt: when,
            username: username
        )
    }

    func testThePillSaysWhatKindOfLoginASiteHas() {
        XCTAssertEqual(ConnectorStatus.pill(connector(kind: "site")), .ready("Ready · no login"))
        XCTAssertEqual(ConnectorStatus.pill(connector(kind: "site", username: "me")), .ready("Ready · passwordless login"))
        XCTAssertEqual(
            ConnectorStatus.pill(connector(kind: "site", username: "me", hasSecret: true)),
            .ready("Ready · login saved")
        )
        XCTAssertEqual(ConnectorStatus.pill(connector(kind: "mcp", status: "needs_auth")), .needsAuth)
        XCTAssertEqual(ConnectorStatus.pill(connector(kind: "mcp", status: "error")), .error)
        XCTAssertEqual(ConnectorStatus.pill(connector(kind: "mcp")), .ready("Authorized"))
    }

    func testAPastedArticleURLBecomesItsSite() {
        XCTAssertEqual(ConnectorStatus.normalizeDomain("  https://www.Example.com:443/a/b?c#d "), "example.com")
        XCTAssertEqual(ConnectorStatus.normalizeDomain("user:pw@news.example.org"), "news.example.org")
        XCTAssertEqual(ConnectorStatus.normalizeDomain(".example.com."), "example.com")
    }

    func testAnIPLiteralIsNotADomain() {
        XCTAssertTrue(ConnectorStatus.looksLikeDomain("example.com"))
        XCTAssertTrue(ConnectorStatus.looksLikeDomain("a-b.example.co.uk"))
        XCTAssertFalse(ConnectorStatus.looksLikeDomain("192.168.0.1"))
        XCTAssertFalse(ConnectorStatus.looksLikeDomain("localhost"))
        XCTAssertFalse(ConnectorStatus.looksLikeDomain("-bad.com"))
    }

    func testTheSitesFieldSplitsOnCommasAndSpaces() {
        XCTAssertEqual(ConnectorStatus.domainList("example.com, https://www.other.org/x  third.net"),
                       ["example.com", "other.org", "third.net"])
        XCTAssertEqual(ConnectorStatus.domainList("  "), [])
    }
}

final class ConsentCallbackTests: XCTestCase {
    private let domain = "app.example.test"

    func testTheRedirectIsTheWebAppsRegisteredCallback() {
        XCTAssertEqual(ConsentCallback.redirectURI(appDomain: "App.Example.test"), "https://app.example.test/oauth/callback")
        XCTAssertNil(ConsentCallback.redirectURI(appDomain: nil))
        XCTAssertNil(ConsentCallback.redirectURI(appDomain: " "))
        // A host is all the build setting may hold; anything else would make a URI Google
        // matches against nothing registered.
        XCTAssertNil(ConsentCallback.redirectURI(appDomain: "app.example.test/evil"))
        XCTAssertNil(ConsentCallback.redirectURI(appDomain: "app.example.test:8443"))
        XCTAssertEqual(ConsentCallback.webSourcesURL(appDomain: domain)?.absoluteString, "https://app.example.test/sources")
    }

    func testAGrantedConsentCarriesItsCodeStateAndIssuer() throws {
        let url = URL(string: "https://app.example.test/oauth/callback?state=s1&code=c1&scope=x&iss=https%3A%2F%2Fas.example")!
        XCTAssertEqual(
            try ConsentCallback.outcome(from: url, appDomain: domain, expectedState: "s1"),
            .granted(code: "c1", state: "s1", iss: "https://as.example")
        )
        let google = URL(string: "https://app.example.test/oauth/callback/?code=c2&state=s2")!
        XCTAssertEqual(
            try ConsentCallback.outcome(from: google, appDomain: domain, expectedState: "s2"),
            .granted(code: "c2", state: "s2", iss: nil)
        )
    }

    func testCancelOnGooglesPageIsAnAnswerNotAFailure() throws {
        let url = URL(string: "https://app.example.test/oauth/callback?error=access_denied&state=s1")!
        let outcome = try ConsentCallback.outcome(from: url, appDomain: domain, expectedState: "s1")
        XCTAssertEqual(outcome, .denied(error: "access_denied", description: ""))
        XCTAssertEqual(
            ConsentCallback.describeDenial(error: "access_denied", description: "", what: "your mailbox"),
            "You didn’t grant access to your mailbox. Nothing was changed."
        )
    }

    func testACallbackForAnotherAttemptIsRefusedBeforeItIsSpent() {
        let url = URL(string: "https://app.example.test/oauth/callback?code=c&state=other")!
        XCTAssertThrowsError(try ConsentCallback.outcome(from: url, appDomain: domain, expectedState: "mine")) {
            XCTAssertEqual($0 as? ConsentCallback.Failure, .stateMismatch)
        }
    }

    func testOnlyTheWebAppsCallbackIsAccepted() {
        for raw in [
            "https://elsewhere.example/oauth/callback?code=c&state=s",
            "http://app.example.test/oauth/callback?code=c&state=s",
            "https://app.example.test/app/signed-in?code=c&state=s",
            "motet://oauth/callback?code=c&state=s",
        ] {
            XCTAssertThrowsError(
                try ConsentCallback.outcome(from: URL(string: raw)!, appDomain: domain, expectedState: "s"), raw
            ) { XCTAssertEqual($0 as? ConsentCallback.Failure, .notTheCallback) }
        }
        XCTAssertThrowsError(
            try ConsentCallback.outcome(
                from: URL(string: "https://app.example.test/oauth/callback")!, appDomain: domain, expectedState: "s"
            )
        ) { XCTAssertEqual($0 as? ConsentCallback.Failure, .empty) }
    }
}

final class SourcesWireTests: XCTestCase {
    private let base = URL(string: "https://api.example.invalid")!

    private func client(_ transport: StubTransport) -> MotetHTTPClient {
        MotetHTTPClient(
            configuration: MotetConfiguration(baseURL: base, apiToken: "session"),
            transport: transport
        )
    }

    private func json(_ request: HTTPRequest) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.body)) as? [String: Any])
    }

    private static let sourceJSON = """
    {"id":"src_1","kind":"gmail","name":"Mail","active":true,"connected":true,
     "created_at":"2026-09-01T00:00:00Z","items_pulled_in":0,"items_integrated":0,"scopes":[]}
    """

    func testConnectingAsksForGmailAtTheWebAppsCallback() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(#"{"source_id":"src_1","authorization_url":"https://accounts.example/o","state":"s1"}"#, status: 201)

        let started = try await client(transport).connectSource(
            name: " Mail ", query: "  ", redirectURI: "https://app.example.test/oauth/callback"
        )

        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.url.path, "/v1/sources/connect")
        XCTAssertEqual(request.headers["Authorization"], "Bearer session")
        let body = try json(request)
        XCTAssertEqual(body["provider"] as? String, "gmail")
        XCTAssertEqual(body["name"] as? String, "Mail")
        XCTAssertEqual(body["redirect_uri"] as? String, "https://app.example.test/oauth/callback")
        // A blank search is Motet's default, which is the API's to apply.
        XCTAssertNil(body["query"])
        XCTAssertEqual(started.state, "s1")
    }

    func testTheConsentFinishesAtTheRouteTheSPAWouldHaveCalled() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(Self.sourceJSON)
        transport.enqueueJSON(#"{"id":"c1","kind":"mcp","label":"M","domains":[],"has_secret":true,"oauth_registered":true,"status":"ready","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}"#)

        _ = try await client(transport).completeSourceConsent(code: "c", state: "s")
        _ = try await client(transport).completeConnectorConsent(code: "c2", state: "connector.s", iss: "https://as.example")

        let requests = transport.recordedRequests()
        XCTAssertEqual(requests.map(\.url.path), ["/v1/sources/callback", "/v1/connectors/oauth/callback"])
        XCTAssertEqual(try json(requests[0]) as NSDictionary, ["code": "c", "state": "s"] as NSDictionary)
        XCTAssertEqual(try json(requests[1])["iss"] as? String, "https://as.example")
    }

    func testClearingALabelSendsNoneRatherThanAnEmptyName() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(Self.sourceJSON)

        _ = try await client(transport).setLabelSync(id: "src_1", removeLabel: " Newsletters ", addLabel: "")

        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.method, "PUT")
        XCTAssertEqual(request.url.path, "/v1/sources/src_1/label-sync")
        let body = try json(request)
        XCTAssertEqual(body["remove_label"] as? String, "Newsletters")
        XCTAssertNil(body["add_label"])
    }

    func testDisconnectAndRemoveAreDeletesWithNoBody() async throws {
        let transport = StubTransport()
        transport.enqueue(.init(status: 204, body: Data()))
        transport.enqueue(.init(status: 204, body: Data()))

        try await client(transport).disconnectSource(id: "src_1")
        try await client(transport).removeSource(id: "src_2")

        let requests = transport.recordedRequests()
        XCTAssertEqual(requests.map(\.method), ["DELETE", "DELETE"])
        XCTAssertEqual(requests.map(\.url.path), ["/v1/sources/src_1/credentials", "/v1/sources/src_2"])
        XCTAssertNil(requests[0].body)
    }

    func testTheAPIsOwnSentenceIsShownNotItsJSON() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(#"{"detail":"Choose a label to add or remove first."}"#, status: 409)

        do {
            _ = try await client(transport).reauthorizeSource(id: "src_1", redirectURI: "https://app.example.test/oauth/callback")
            XCTFail("expected a refusal")
        } catch let error as MotetError {
            guard case .http(409, let detail) = error else { return XCTFail("got \(error)") }
            XCTAssertEqual(detail, "Choose a label to add or remove first.")
        }
    }

    func testAConnectorIsCreatedWithTheRiskAcknowledgementItsKindNeeds() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(#"{"id":"c1","kind":"mcp","label":"M","url":"https://mcp.example","domains":["example.com"],"has_secret":false,"oauth_registered":false,"status":"needs_auth","created_at":"2026-09-01T00:00:00Z","updated_at":"2026-09-01T00:00:00Z"}"#, status: 201)

        let created = try await client(transport).createConnector(
            CreateConnectorRequest(acknowledgeRisk: true, domains: ["example.com"], kind: "mcp", url: "https://mcp.example")
        )

        let body = try json(XCTUnwrap(transport.recordedRequests().first))
        XCTAssertEqual(body["kind"] as? String, "mcp")
        XCTAssertEqual(body["acknowledge_risk"] as? Bool, true)
        XCTAssertEqual(created.status, "needs_auth")
    }
}
