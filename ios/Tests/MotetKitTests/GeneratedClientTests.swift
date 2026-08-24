import XCTest
@testable import MotetKit

/// The generated half of the contract. `bin/ci` proves the file is in sync with
/// `openapi.yaml`; these prove the types it emits actually decode what the API sends.
final class GeneratedClientTests: XCTestCase {
    func testAnEpisodeDecodesFromTheContractsShape() throws {
        let json = """
        {
          "id": "ep-1", "title": "Morning briefing", "state": "ready",
          "duration_ms": 300000, "max_duration_ms": 600000,
          "audio_bytes": 4820000, "audio_media_type": "audio/mpeg", "last_error": null,
          "created_at": "2026-08-24T04:00:00.123456Z", "published_at": null,
          "segments": [{
            "news_item_id": "news-a", "news_item_title": "Alpha", "text": "…",
            "start_ms": 0, "duration_ms": 60000,
            "claims": [{
              "text": "They raised twelve million dollars.",
              "span": {"source_item_id": "src-1", "start": 20, "end": 40},
              "source_excerpt": "raised $12m", "source_title": "Newsletter"
            }]
          }]
        }
        """
        let episode = try MotetDate.makeDecoder().decode(
            EpisodeResponse.self, from: Data(json.utf8)
        )

        XCTAssertEqual(episode.episodeState, .ready)
        XCTAssertEqual(episode.audioBytes, 4_820_000)
        XCTAssertNil(episode.publishedAt)
        XCTAssertEqual(episode.segments.first?.claims.first?.span.start, 20)
        XCTAssertEqual(episode.newsItemIds, ["news-a"])
    }

    func testAnUnknownStateRendersRatherThanCrashing() {
        // The app ships through App Store review; the API does not. A state added
        // server-side must not brick an installed build.
        let episode = Fixture.episode(state: "transcoding")
        XCTAssertEqual(episode.episodeState, .unknown("transcoding"))
        XCTAssertFalse(episode.episodeState.isPlayable)
        XCTAssertEqual(episode.episodeState.displayName, "Transcoding")
    }

    func testEndpointsMatchTheContractsPaths() {
        XCTAssertEqual(MotetEndpoints.listEpisodes.path, "/v1/episodes")
        XCTAssertEqual(MotetEndpoints.listNewsItems.path, "/v1/news-items")
        XCTAssertEqual(MotetEndpoints.getFeedInfo.path, "/v1/feed")
        XCTAssertEqual(
            MotetEndpoints.markEpisodeListened(episodeId: "e").path, "/v1/episodes/e/listened"
        )
        XCTAssertEqual(MotetEndpoints.createEpisode.method, "POST")
    }

    func testValidationDetailDecodesItsUntypedCorners() throws {
        let json = #"{"detail":[{"loc":["body",0],"msg":"bad","type":"x","ctx":{"limit":5},"input":null}]}"#
        let error = try JSONDecoder().decode(HTTPValidationError.self, from: Data(json.utf8))
        XCTAssertEqual(error.detail?.first?.loc.map(\.displayText), ["body", "0"])
    }
}
