import XCTest
@testable import MotetKit

/// The wire: what the app actually sends, and what it does with what comes back.
final class MotetHTTPClientTests: XCTestCase {
    private let base = URL(string: "https://api.example.invalid")!

    private func makeClient(_ transport: StubTransport) -> MotetHTTPClient {
        MotetHTTPClient(
            configuration: MotetConfiguration(baseURL: base, apiToken: "secret", feedToken: "feed"),
            transport: transport
        )
    }

    func testEveryV1CallCarriesTheBearerToken() async throws {
        let transport = StubTransport()
        transport.enqueueJSON("[]")
        _ = try await makeClient(transport).listEpisodes()

        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.url.absoluteString, "https://api.example.invalid/v1/episodes")
        XCTAssertEqual(request.headers["Authorization"], "Bearer secret")
        XCTAssertEqual(request.method, "GET")
    }

    func testReadStateSendsTheContractsBody() async throws {
        let transport = StubTransport()
        transport.enqueueJSON("""
        {"id":"n1","title":"T","summary":"S","source_item_ids":[],"sources":[],"read":true,
         "created_at":"2026-08-24T04:00:00.123456Z"}
        """)

        let item = try await makeClient(transport).setNewsItemRead(id: "n1", read: true)

        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(
            request.url.absoluteString, "https://api.example.invalid/v1/news-items/n1/read"
        )
        XCTAssertEqual(request.headers["Content-Type"], "application/json")
        XCTAssertEqual(String(data: try XCTUnwrap(request.body), encoding: .utf8), #"{"read":true}"#)
        XCTAssertTrue(item.read)
    }

    private static let pendingEpisode = """
    {"id":"e1","title":"Picked","state":"pending","duration_ms":0,"max_duration_ms":1800000,
     "audio_bytes":null,"audio_media_type":null,"last_error":null,
     "created_at":"2026-09-19T04:00:00.123456Z","published_at":null,
     "listened_through_ms":0,"keep_in_backlog":true,"segments":[]}
    """

    private func sentBody(_ transport: StubTransport) throws -> [String: Any] {
        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.url.absoluteString, "https://api.example.invalid/v1/episodes")
        let body = try JSONSerialization.jsonObject(with: try XCTUnwrap(request.body))
        return try XCTUnwrap(body as? [String: Any])
    }

    func testAPickedEpisodeSendsItsItemsAndTheKeepFlag() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(Self.pendingEpisode, status: 201)

        let episode = try await makeClient(transport).createEpisode(
            title: "Picked", maxDurationMs: 1_800_000, newsItemIds: ["n1", "n2"], keepInBacklog: true
        )

        let body = try sentBody(transport)
        XCTAssertEqual(body["news_item_ids"] as? [String], ["n1", "n2"])
        XCTAssertEqual(body["keep_in_backlog"] as? Bool, true)
        XCTAssertEqual(body["max_duration_ms"] as? Int, 1_800_000)
        XCTAssertTrue(episode.keepsStoriesInBacklog)
    }

    func testTheWholeBacklogRequestIsUnchanged() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(Self.pendingEpisode, status: 201)

        _ = try await makeClient(transport).createEpisode(title: "All", maxDurationMs: 60_000)

        let body = try sentBody(transport)
        XCTAssertEqual(Set(body.keys), ["title", "max_duration_ms"])
    }

    func testAnEpisodeWithoutTheFlagDecodesAsConsuming() async throws {
        // What an API older than the field sends, and what an offline cache written by an
        // older build holds. Neither may fail to decode.
        let transport = StubTransport()
        transport.enqueueJSON(
            Self.pendingEpisode.replacingOccurrences(of: #""keep_in_backlog":true,"#, with: "")
        )
        let episode = try await makeClient(transport).episode(id: "e1")
        XCTAssertNil(episode.keepInBacklog)
        XCTAssertFalse(episode.keepsStoriesInBacklog)
    }

    func testAnIdWithASlashCannotRewriteTheRoute() async throws {
        let transport = StubTransport()
        transport.enqueueJSON("{}")
        _ = try? await makeClient(transport).episode(id: "../../admin")

        let request = try XCTUnwrap(transport.recordedRequests().first)
        XCTAssertEqual(
            request.url.absoluteString,
            "https://api.example.invalid/v1/episodes/..%2F..%2Fadmin"
        )
    }

    func testTheAudioURLCarriesTheFeedTokenNotTheBearer() throws {
        let url = try makeClient(StubTransport()).audioURL(episodeId: "ep-1", feedToken: "feed")
        XCTAssertEqual(
            url.absoluteString,
            "https://api.example.invalid/v1/episodes/ep-1/audio?token=feed"
        )
    }

    func testABaseURLWithAPathPrefixIsPreserved() throws {
        let client = MotetHTTPClient(
            configuration: MotetConfiguration(
                baseURL: URL(string: "https://example.invalid/motet")!, apiToken: "t"
            ),
            transport: StubTransport()
        )
        let url = try client.audioURL(episodeId: "ep-1", feedToken: "feed")
        XCTAssertEqual(
            url.absoluteString,
            "https://example.invalid/motet/v1/episodes/ep-1/audio?token=feed"
        )
    }

    func testMicrosecondTimestampsDecode() async throws {
        // Pydantic serialises datetimes with microseconds; ISO8601DateFormatter does not
        // accept them. This is the shape the API actually emits.
        let transport = StubTransport()
        transport.enqueueJSON("""
        [{"id":"n1","title":"T","summary":"S","source_item_ids":[],"sources":[],"read":false,
          "created_at":"2026-08-24T04:00:00.123456+00:00"}]
        """)
        let items = try await makeClient(transport).listNewsItems()
        XCTAssertEqual(items.count, 1)
        XCTAssertEqual(
            items[0].createdAt.timeIntervalSince1970,
            Date(timeIntervalSince1970: 1_787_544_000.123).timeIntervalSince1970,
            accuracy: 0.01
        )
    }

    func testATimestampWithNoOffsetIsReadAsUTC() throws {
        let parsed = try XCTUnwrap(MotetDate.parse("2026-08-24T04:00:00.123456"))
        XCTAssertEqual(
            parsed.timeIntervalSince1970,
            Date(timeIntervalSince1970: 1_787_544_000.123).timeIntervalSince1970,
            accuracy: 0.01
        )
    }

    func testA401BecomesUnauthorizedRatherThanAGenericFailure() async throws {
        let transport = StubTransport()
        transport.enqueueJSON(#"{"detail":"nope"}"#, status: 401)
        do {
            _ = try await makeClient(transport).listEpisodes()
            XCTFail("expected a failure")
        } catch let error as MotetError {
            guard case .unauthorized = error else { return XCTFail("got \(error)") }
            XCTAssertFalse(error.isRetryable, "a bad token is not fixed by waiting")
        }
    }

    func testA422SurfacesTheFieldThatWasWrong() async throws {
        let transport = StubTransport()
        transport.enqueueJSON("""
        {"detail":[{"loc":["body","title"],"msg":"field required","type":"missing"}]}
        """, status: 422)
        do {
            _ = try await makeClient(transport).createEpisode(title: "", maxDurationMs: 1)
            XCTFail("expected a failure")
        } catch let error as MotetError {
            XCTAssertEqual(error.description, "HTTP 422: body.title: field required")
            XCTAssertFalse(error.isRetryable)
        }
    }

    func testAFlatNetworkIsOfflineAndRetryable() async throws {
        let transport = StubTransport([
            StubTransport.Exchange(error: URLError(.notConnectedToInternet))
        ])
        do {
            _ = try await makeClient(transport).listEpisodes()
            XCTFail("expected a failure")
        } catch let error as MotetError {
            guard case .offline = error else { return XCTFail("got \(error)") }
            XCTAssertTrue(error.isRetryable)
        }
    }

    func testA503IsRetryable() {
        XCTAssertTrue(MotetError.http(status: 503, detail: nil).isRetryable)
        XCTAssertFalse(MotetError.http(status: 404, detail: nil).isRetryable)
    }

    func testAnUnconfiguredClientSaysSoRatherThanBuildingANonsenseURL() async throws {
        let client = MotetHTTPClient(
            configuration: MotetConfiguration(), transport: StubTransport()
        )
        do {
            _ = try await client.listEpisodes()
            XCTFail("expected a failure")
        } catch let error as MotetError {
            guard case .notConfigured = error else { return XCTFail("got \(error)") }
        }
    }
}
