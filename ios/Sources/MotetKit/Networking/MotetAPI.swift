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
    /// `newsItemIds` nil is "every unread item"; a list is exactly those stories.
    /// `keepInBacklog` makes listening to the episode leave their read state alone.
    func createEpisode(
        title: String, maxDurationMs: Int, newsItemIds: [String]?, keepInBacklog: Bool
    ) async throws -> EpisodeResponse
    func markEpisodeListened(id: String) async throws -> MarkListenedResponse
    /// Move the server's listening position (`PUT /v1/episodes/{id}/position`). Monotonic
    /// on the server: a smaller value is accepted and changes nothing.
    func setPlaybackPosition(episodeId: String, listenedThroughMs: Int) async throws -> ListenProgressResponse

    func listNewsItems() async throws -> [NewsItemResponse]
    func setNewsItemRead(id: String, read: Bool) async throws -> NewsItemResponse

    func pasteSource(title: String, text: String) async throws -> SourceItemResponse

    /// The feed token, which is also what authenticates an audio download.
    func feedInfo() async throws -> FeedInfoResponse

    /// Where an episode's audio lives, for the downloader.
    func audioURL(episodeId: String, feedToken: String) throws -> URL

    /// Why an episode's audio would not load, asked of the audio route itself — the player's
    /// own error carries no HTTP status, so "gone" and "this phone can't play it" look the
    /// same from there. Nil when the route could not be asked.
    func audioProblem(episodeId: String, feedToken: String) async -> AudioProblem?
}

/// What the audio route says about audio that would not play.
public enum AudioProblem: Equatable, Sendable {
    /// The file is gone — the API's 410 since motet#129, or a signed URL into a 404 before
    /// it. `reason` is the API's own sentence when it sent one.
    case gone(reason: String?)
    /// The feed token was refused: it was rotated since this phone cached it.
    case feedTokenRefused
    /// The route serves the file, so it was the player that could not play it.
    case unplayable

    public var sentence: String {
        switch self {
        case .gone(let reason?): return reason
        case .gone(nil): return "This episode's audio is no longer in storage. Its stories are still in your backlog."
        case .feedTokenRefused: return "The feed link changed since this phone last asked. Try again."
        case .unplayable: return "The audio is there, but this phone could not play it."
        }
    }
}

extension MotetAPI {
    public func audioProblem(episodeId: String, feedToken: String) async -> AudioProblem? { nil }
}

extension MotetAPI {
    /// Every unread item, consumed as it is heard — what "New episode" has always made.
    public func createEpisode(title: String, maxDurationMs: Int) async throws -> EpisodeResponse {
        try await createEpisode(
            title: title, maxDurationMs: maxDurationMs, newsItemIds: nil, keepInBacklog: false
        )
    }
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

    public func createEpisode(
        title: String, maxDurationMs: Int, newsItemIds: [String]?, keepInBacklog: Bool
    ) async throws -> EpisodeResponse {
        // `keep_in_backlog` is sent only when it is on, so the whole-backlog request is
        // byte-for-byte what it was before picking existed.
        let body = CreateEpisodeRequest(
            keepInBacklog: keepInBacklog ? true : nil,
            maxDurationMs: maxDurationMs,
            newsItemIds: newsItemIds,
            title: title
        )
        return try await send(MotetEndpoints.createEpisode, body: body, as: EpisodeResponse.self)
    }

    public func markEpisodeListened(id: String) async throws -> MarkListenedResponse {
        try await send(
            MotetEndpoints.markEpisodeListened(episodeId: id), as: MarkListenedResponse.self
        )
    }

    public func setPlaybackPosition(
        episodeId: String, listenedThroughMs: Int
    ) async throws -> ListenProgressResponse {
        try await send(
            MotetEndpoints.setPlaybackPosition(episodeId: episodeId),
            body: ListenProgressRequest(listenedThroughMs: listenedThroughMs),
            as: ListenProgressResponse.self
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

    /// Two bytes of the audio, redirects followed: enough to learn the status without
    /// downloading an episode.
    public func audioProblem(episodeId: String, feedToken: String) async -> AudioProblem? {
        guard let url = try? audioURL(episodeId: episodeId, feedToken: feedToken),
              let response = try? await transport.send(
                  HTTPRequest(url: url, method: "GET", headers: ["Range": "bytes=0-1"])
              )
        else { return nil }
        switch response.statusCode {
        case 200..<300:
            return .unplayable
        case 401, 403:
            return .feedTokenRefused
        case 404, 410:
            let detail = (try? decoder.decode(AudioDetail.self, from: response.body))?.detail
            return .gone(reason: detail.flatMap { $0.isEmpty ? nil : $0 })
        default:
            return nil
        }
    }

    // MARK: - Signing in

    /// Begin the web sign-in on this app's behalf (see `NativeSignIn`). Unauthenticated:
    /// it is how an app that holds nothing gets something.
    public func startNativeSignIn(
        codeChallenge: String, appLinkDomain: String? = nil
    ) async throws -> StartNativeLoginResponse {
        try await send(
            MotetEndpoints.startNativeLogin,
            body: StartNativeLoginRequest(
                appLinkDomain: appLinkDomain, codeChallenge: codeChallenge
            ),
            as: StartNativeLoginResponse.self
        )
    }

    /// Trade the handoff link's code, and the verifier only this app holds, for a session.
    public func redeemNativeSignIn(code: String, codeVerifier: String) async throws -> LoginResponse {
        try await send(
            MotetEndpoints.redeemNativeLogin,
            body: RedeemNativeLoginRequest(code: code, codeVerifier: codeVerifier),
            as: LoginResponse.self
        )
    }

    /// Revoke this app's session on the server. A no-op for the shared API token.
    public func signOut() async throws {
        _ = try await perform(MotetEndpoints.logout, body: Optional<Never>.none)
    }

    // MARK: - Plumbing

    func send<Response: Decodable>(
        _ endpoint: HTTPEndpoint, as type: Response.Type
    ) async throws -> Response {
        try await send(endpoint, body: Optional<Never>.none, as: type)
    }

    func send<Body: Encodable, Response: Decodable>(
        _ endpoint: HTTPEndpoint, body: Body?, as _: Response.Type
    ) async throws -> Response {
        let response = try await perform(endpoint, body: body)
        do {
            return try decoder.decode(Response.self, from: response.body)
        } catch {
            throw MotetError.decoding(String(describing: error))
        }
    }

    func perform<Body: Encodable>(
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

    /// FastAPI's 422 body is a list of field errors, and every other refusal it raises is
    /// `{"detail": "<sentence>"}` — the API's own words, which a screen should show rather
    /// than the JSON around them. Anything else is shown verbatim.
    private static func validationDetail(_ response: HTTPResponse, decoder: JSONDecoder) -> String? {
        if let error = try? decoder.decode(HTTPValidationError.self, from: response.body),
           let detail = error.detail, !detail.isEmpty {
            return detail.map { entry in
                let field = entry.loc.map(\.displayText).joined(separator: ".")
                return field.isEmpty ? entry.msg : "\(field): \(entry.msg)"
            }.joined(separator: "; ")
        }
        if let error = try? decoder.decode(DetailMessage.self, from: response.body),
           !error.detail.isEmpty {
            return error.detail
        }
        let text = String(data: response.body, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (text?.isEmpty ?? true) ? nil : text
    }
}

/// The API's `{"detail": "<sentence>"}` on the audio route's 410.
private struct AudioDetail: Decodable {
    let detail: String
}

/// The body of every `HTTPException` the API raises: one sentence, meant for a person.
private struct DetailMessage: Decodable {
    let detail: String
}
