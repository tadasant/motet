import Foundation

/// Everything the app asks of Motet's API — and the only vendor-shaped surface it has.
///
/// Product invariant 1: no client speaks a vendor protocol. There is no OpenAI, Cartesia,
/// or Anthropic call anywhere in this app, and there is no credential for one. Audio comes
/// from `/v1/episodes/{id}/audio`, which either serves the bytes or redirects to a signed
/// URL — the client cannot tell, and must not care.
public protocol MotetAPI: Sendable {
    func listEpisodes() async throws -> [EpisodeResponse]
    func episode(id: String) async throws -> EpisodeResponse
    func createEpisode(title: String, maxDurationMs: Int) async throws -> EpisodeResponse
    func markEpisodeListened(id: String) async throws -> MarkListenedResponse

    func listNewsItems() async throws -> [NewsItemResponse]
    func setNewsItemRead(id: String, read: Bool) async throws -> NewsItemResponse

    func pasteSource(title: String, text: String) async throws -> SourceItemResponse

    /// The feed token, which is also what authenticates an audio download.
    func feedInfo() async throws -> FeedInfoResponse

    /// Where an episode's audio lives, for the downloader.
    func audioURL(episodeId: String, feedToken: String) throws -> URL
}

/// The HTTP implementation.
///
/// Authentication is applied here and nowhere else: `/v1` takes a bearer token, and the
/// audio route takes the feed token in the query string instead, because that route is the
/// one a podcast client also uses. Both are read from `MotetConfiguration`, so no call site
/// chooses.
public struct MotetHTTPClient: MotetAPI {
    private let configuration: MotetConfiguration
    private let transport: any HTTPTransport
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    public init(configuration: MotetConfiguration, transport: any HTTPTransport = URLSessionTransport()) {
        self.configuration = configuration
        self.transport = transport
        self.decoder = MotetDate.makeDecoder()
        self.encoder = MotetDate.makeEncoder()
    }

    // MARK: - Episodes

    public func listEpisodes() async throws -> [EpisodeResponse] {
        try await send(MotetEndpoints.listEpisodes, as: [EpisodeResponse].self)
    }

    public func episode(id: String) async throws -> EpisodeResponse {
        try await send(MotetEndpoints.getEpisode(episodeId: id), as: EpisodeResponse.self)
    }

    public func createEpisode(title: String, maxDurationMs: Int) async throws -> EpisodeResponse {
        let body = CreateEpisodeRequest(maxDurationMs: maxDurationMs, title: title)
        return try await send(MotetEndpoints.createEpisode, body: body, as: EpisodeResponse.self)
    }

    public func markEpisodeListened(id: String) async throws -> MarkListenedResponse {
        try await send(
            MotetEndpoints.markEpisodeListened(episodeId: id), as: MarkListenedResponse.self
        )
    }

    // MARK: - Backlog

    public func listNewsItems() async throws -> [NewsItemResponse] {
        try await send(MotetEndpoints.listNewsItems, as: [NewsItemResponse].self)
    }

    public func setNewsItemRead(id: String, read: Bool) async throws -> NewsItemResponse {
        try await send(
            MotetEndpoints.setNewsItemRead(newsItemId: id),
            body: ReadStateRequest(read: read),
            as: NewsItemResponse.self
        )
    }

    public func pasteSource(title: String, text: String) async throws -> SourceItemResponse {
        try await send(
            MotetEndpoints.pasteSource,
            body: PasteRequest(text: text, title: title),
            as: SourceItemResponse.self
        )
    }

    // MARK: - Feed

    public func feedInfo() async throws -> FeedInfoResponse {
        try await send(MotetEndpoints.getFeedInfo, as: FeedInfoResponse.self)
    }

    public func audioURL(episodeId: String, feedToken: String) throws -> URL {
        let endpoint = MotetEndpoints.episodeAudio(episodeId: episodeId, token: feedToken)
        guard let base = configuration.baseURL, let url = endpoint.url(relativeTo: base) else {
            throw MotetError.notConfigured
        }
        return url
    }

    // MARK: - Plumbing

    private func send<Response: Decodable>(
        _ endpoint: HTTPEndpoint, as type: Response.Type
    ) async throws -> Response {
        try await send(endpoint, body: Optional<Never>.none, as: type)
    }

    private func send<Body: Encodable, Response: Decodable>(
        _ endpoint: HTTPEndpoint, body: Body?, as _: Response.Type
    ) async throws -> Response {
        let response = try await perform(endpoint, body: body)
        do {
            return try decoder.decode(Response.self, from: response.body)
        } catch {
            throw MotetError.decoding(String(describing: error))
        }
    }

    private func perform<Body: Encodable>(
        _ endpoint: HTTPEndpoint, body: Body?
    ) async throws -> HTTPResponse {
        guard let base = configuration.baseURL, let url = endpoint.url(relativeTo: base) else {
            throw MotetError.notConfigured
        }

        var headers = ["Accept": "application/json"]
        if let token = configuration.apiToken, !token.isEmpty {
            headers["Authorization"] = "Bearer \(token)"
        }
        var encodedBody: Data?
        if let body {
            encodedBody = try encoder.encode(body)
            headers["Content-Type"] = "application/json"
        }

        let request = HTTPRequest(
            url: url, method: endpoint.method, headers: headers, body: encodedBody
        )

        let response: HTTPResponse
        do {
            response = try await transport.send(request)
        } catch let error as MotetError {
            throw error
        } catch {
            throw Self.mapTransportError(error)
        }

        guard (200..<300).contains(response.statusCode) else {
            throw Self.mapStatus(response, decoder: decoder)
        }
        return response
    }

    /// A flat network is `offline`, not a crash-worthy surprise. See `MotetError`.
    static func mapTransportError(_ error: Error) -> MotetError {
        let code = (error as NSError).code
        let offlineCodes: Set<Int> = [
            URLError.notConnectedToInternet.rawValue,
            URLError.networkConnectionLost.rawValue,
            URLError.cannotConnectToHost.rawValue,
            URLError.cannotFindHost.rawValue,
            URLError.dataNotAllowed.rawValue,
            URLError.timedOut.rawValue,
            URLError.internationalRoamingOff.rawValue,
            URLError.secureConnectionFailed.rawValue,
        ]
        if (error as NSError).domain == NSURLErrorDomain, offlineCodes.contains(code) {
            return .offline
        }
        return .transport(error)
    }

    static func mapStatus(_ response: HTTPResponse, decoder: JSONDecoder) -> MotetError {
        if response.statusCode == 401 || response.statusCode == 403 {
            return .unauthorized
        }
        return .http(status: response.statusCode, detail: validationDetail(response, decoder: decoder))
    }

    /// FastAPI's 422 body is a list of field errors; anything else is shown verbatim.
    private static func validationDetail(_ response: HTTPResponse, decoder: JSONDecoder) -> String? {
        if let error = try? decoder.decode(HTTPValidationError.self, from: response.body),
           let detail = error.detail, !detail.isEmpty {
            return detail.map { entry in
                let field = entry.loc.map(\.displayText).joined(separator: ".")
                return field.isEmpty ? entry.msg : "\(field): \(entry.msg)"
            }.joined(separator: "; ")
        }
        let text = String(data: response.body, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (text?.isEmpty ?? true) ? nil : text
    }
}
