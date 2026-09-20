import Foundation

/// What the Sources screen asks of Motet's API: the accounts content comes from, what they
/// have pulled in, and the connectors enrichment may use.
///
/// Its own protocol rather than more of `MotetAPI`, because nothing that plays audio needs
/// any of it — the library, the outbox and the offline store stay exactly as small as they
/// were — and because a test of the screen's logic can then fake eleven calls rather than
/// thirty. Every route here is one the SPA's Sources and Credentials screens already call;
/// the app adds no API surface of its own.
public protocol SourcesAPI: Sendable {
    func listSources() async throws -> [SourceResponse]
    func heldSourceItems() async throws -> [HeldSourceItemResponse]
    func ingestion() async throws -> [IngestionItemResponse]
    func processingStatus() async throws -> ProcessingStatusResponse

    /// Start connecting a mailbox. `redirectURI` is the web app's `/oauth/callback`,
    /// the one address registered on the Google OAuth client (see `ConsentCallback`).
    func connectSource(
        name: String, query: String?, redirectURI: String, firstSyncDays: Int?
    ) async throws -> ConnectSourceResponse
    /// Finish connecting (or re-authorizing) a mailbox with the code Google returned.
    func completeSourceConsent(code: String, state: String) async throws -> SourceResponse
    /// Queue a poll now. Answers at once; the sync has *run* when `last_sync.at` moves.
    func pollSource(id: String) async throws -> SourceResponse
    /// Search this mailbox again from `days` ago, and keep that as its window (motet#139).
    ///
    /// The repair for a first sync that did not reach far enough. `pollSource` looks
    /// *forward* from the watermark and can never pull in older mail; this is the only call
    /// that reaches backwards. Mail already pulled in is skipped before it is fetched.
    func resyncSource(id: String, days: Int) async throws -> SourceResponse
    /// Spend inference on held items: the gate between free work and paid work (motet#91).
    func integrateSourceItems(ids: [String]) async throws -> IntegrateResponse
    /// Discard held items without spending on them. Nothing un-dismisses.
    func dismissSourceItems(ids: [String]) async throws -> DismissResponse
    /// Forget the mailbox's credential. What it pulled in stays.
    func disconnectSource(id: String) async throws
    /// Remove a consent attempt that never finished. Refused for anything that ever connected.
    func removeSource(id: String) async throws
    /// Label sync (motet#96). An empty or nil name is "none".
    func setLabelSync(id: String, removeLabel: String?, addLabel: String?) async throws -> SourceResponse
    /// Ask for the wider `gmail.modify` consent label sync needs. 409 until labels are set.
    func reauthorizeSource(id: String, redirectURI: String) async throws -> ConnectSourceResponse

    func listConnectors() async throws -> [ConnectorResponse]
    func createConnector(_ request: CreateConnectorRequest) async throws -> ConnectorResponse
    func deleteConnector(id: String) async throws
    func authorizeConnector(id: String, redirectURI: String) async throws -> AuthorizeConnectorResponse
    func completeConnectorConsent(code: String, state: String, iss: String?) async throws -> ConnectorResponse
}

extension MotetHTTPClient: SourcesAPI {
    public func listSources() async throws -> [SourceResponse] {
        try await send(MotetEndpoints.listSources, as: [SourceResponse].self)
    }

    public func heldSourceItems() async throws -> [HeldSourceItemResponse] {
        try await send(MotetEndpoints.listHeldSourceItems, as: [HeldSourceItemResponse].self)
    }

    public func ingestion() async throws -> [IngestionItemResponse] {
        try await send(MotetEndpoints.listIngestion, as: [IngestionItemResponse].self)
    }

    public func processingStatus() async throws -> ProcessingStatusResponse {
        try await send(MotetEndpoints.processingStatus, as: ProcessingStatusResponse.self)
    }

    public func connectSource(
        name: String, query: String?, redirectURI: String, firstSyncDays: Int?
    ) async throws -> ConnectSourceResponse {
        let trimmed = query?.trimmingCharacters(in: .whitespacesAndNewlines)
        return try await send(
            MotetEndpoints.connectSource,
            body: ConnectSourceRequest(
                firstSyncDays: firstSyncDays,
                name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                provider: "gmail",
                query: (trimmed?.isEmpty ?? true) ? nil : trimmed,
                redirectUri: redirectURI
            ),
            as: ConnectSourceResponse.self
        )
    }

    public func completeSourceConsent(code: String, state: String) async throws -> SourceResponse {
        try await send(
            MotetEndpoints.oauthCallback,
            body: OAuthCallbackRequest(code: code, state: state),
            as: SourceResponse.self
        )
    }

    public func pollSource(id: String) async throws -> SourceResponse {
        try await send(MotetEndpoints.pollSource(sourceId: id), as: SourceResponse.self)
    }

    public func resyncSource(id: String, days: Int) async throws -> SourceResponse {
        try await send(
            MotetEndpoints.resyncSource(sourceId: id),
            body: ResyncRequest(firstSyncDays: days),
            as: SourceResponse.self
        )
    }

    public func integrateSourceItems(ids: [String]) async throws -> IntegrateResponse {
        try await send(
            MotetEndpoints.integrateSourceItems,
            body: SourceItemIdsRequest(ids: ids),
            as: IntegrateResponse.self
        )
    }

    public func dismissSourceItems(ids: [String]) async throws -> DismissResponse {
        try await send(
            MotetEndpoints.dismissSourceItems,
            body: SourceItemIdsRequest(ids: ids),
            as: DismissResponse.self
        )
    }

    public func disconnectSource(id: String) async throws {
        _ = try await perform(MotetEndpoints.disconnectSource(sourceId: id), body: Optional<Never>.none)
    }

    public func removeSource(id: String) async throws {
        _ = try await perform(MotetEndpoints.removeSource(sourceId: id), body: Optional<Never>.none)
    }

    public func setLabelSync(
        id: String, removeLabel: String?, addLabel: String?
    ) async throws -> SourceResponse {
        try await send(
            MotetEndpoints.setLabelSync(sourceId: id),
            body: LabelSyncRequest(
                addLabel: Self.nonEmpty(addLabel), removeLabel: Self.nonEmpty(removeLabel)
            ),
            as: SourceResponse.self
        )
    }

    public func reauthorizeSource(id: String, redirectURI: String) async throws -> ConnectSourceResponse {
        try await send(
            MotetEndpoints.reauthorizeSource(sourceId: id),
            body: ReauthorizeSourceRequest(redirectUri: redirectURI),
            as: ConnectSourceResponse.self
        )
    }

    public func listConnectors() async throws -> [ConnectorResponse] {
        try await send(MotetEndpoints.listConnectors, as: [ConnectorResponse].self)
    }

    public func createConnector(_ request: CreateConnectorRequest) async throws -> ConnectorResponse {
        try await send(MotetEndpoints.createConnector, body: request, as: ConnectorResponse.self)
    }

    public func deleteConnector(id: String) async throws {
        _ = try await perform(MotetEndpoints.deleteConnector(connectorId: id), body: Optional<Never>.none)
    }

    public func authorizeConnector(id: String, redirectURI: String) async throws -> AuthorizeConnectorResponse {
        try await send(
            MotetEndpoints.authorizeConnector(connectorId: id),
            body: AuthorizeConnectorRequest(redirectUri: redirectURI),
            as: AuthorizeConnectorResponse.self
        )
    }

    public func completeConnectorConsent(
        code: String, state: String, iss: String?
    ) async throws -> ConnectorResponse {
        try await send(
            MotetEndpoints.connectorOauthCallback,
            body: ConnectorOAuthCallbackRequest(code: code, iss: iss, state: state),
            as: ConnectorResponse.self
        )
    }

    private static func nonEmpty(_ value: String?) -> String? {
        let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty ? nil : trimmed
    }
}
