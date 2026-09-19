import Foundation
import MotetKit

/// What the Sources tab observes: the accounts content comes from, what each has pulled in,
/// and the connectors enrichment may use. The rules are `SourceStatus` and
/// `ConnectorStatus`, in MotetKit; this is loading, and the actions.
@MainActor
final class SourcesModel: ObservableObject {
    @Published private(set) var sources: [SourceResponse]?
    @Published private(set) var held: [HeldSourceItemResponse] = []
    @Published private(set) var ingestion: [IngestionItemResponse] = []
    @Published private(set) var processing: ProcessingStatusResponse?
    @Published private(set) var connectors: [ConnectorResponse]?
    /// Why the primary list could not be loaded. The screen keeps what it had underneath.
    @Published private(set) var loadError: String?
    /// One sentence about the last consent, shown at the top — including "you pressed
    /// Cancel", which the row it left behind cannot say.
    @Published var notice: String?
    /// Set when a consent could not be opened on this phone, so the screen offers the web app.
    @Published var consentNeedsWebApp = false
    @Published private(set) var busy = false

    private let app: AppModel

    init(app: AppModel) {
        self.app = app
    }

    private var api: any SourcesAPI { app.sourcesAPI }
    var appLinkDomain: String? { app.appLinkDomain }
    var webSourcesURL: URL? { ConsentCallback.webSourcesURL(appDomain: appLinkDomain) }
    var canConsentHere: Bool { ConsentSheet.isAvailable(appDomain: appLinkDomain) }

    func rows(_ integration: Integration) -> [SourceResponse] {
        integration.rows(in: sources ?? [])
    }

    func source(id: String) -> SourceResponse? {
        sources?.first { $0.id == id }
    }

    func counts(for source: SourceResponse) -> SourceStatus.Counts {
        SourceStatus.counts(for: source, held: held, ingestion: ingestion)
    }

    var worker: SourceStatus.Worker { SourceStatus.worker(processing) }

    /// Whether anything pulled in is still moving, so the counts are worth re-reading.
    var inFlight: Bool { ingestion.contains { $0.state == "pending" } }

    // MARK: - Loading

    /// The sources list is the primary fetch and the only one that can blank the screen.
    /// Held, ingestion, processing and connectors are each best-effort: a failure loses a
    /// count or a section, never the catalog.
    func refresh() async {
        let api = self.api
        async let list = Self.catching { try await api.listSources() }
        async let heldItems = try? api.heldSourceItems()
        async let ingestionItems = try? api.ingestion()
        async let processingStatus = try? api.processingStatus()
        async let connectorList = try? api.listConnectors()

        switch await list {
        case .success(let fresh):
            sources = fresh
            loadError = nil
        case .failure(let error):
            // A failed re-fetch keeps the rows on screen: replacing them with nothing would
            // read as a fresh account on one transient error.
            loadError = Self.describe(error)
            if sources == nil { sources = [] }
        }
        held = await heldItems ?? held
        ingestion = await ingestionItems ?? ingestion
        processing = await processingStatus
        if let fresh = await connectorList { connectors = fresh }
        else if connectors == nil { connectors = [] }
    }

    // MARK: - Mailboxes

    /// Connect a mailbox: mint the source and consent URL, show Google's page in the sheet,
    /// and finish the consent with this phone's own session.
    func connectMailbox(
        name: String, query: String, present: @MainActor (URL) async -> ConsentSheet.Result
    ) async -> Bool {
        guard let redirect = ConsentCallback.redirectURI(appDomain: appLinkDomain), canConsentHere else {
            consentNeedsWebApp = true
            return false
        }
        return await runConsent(what: "your mailbox", present: present) { api in
            let started = try await api.connectSource(name: name, query: query, redirectURI: redirect)
            return (started.authorizationUrl, started.state)
        } finish: { api, code, state, _ in
            let source = try await api.completeSourceConsent(code: code, state: state)
            return "\(source.name) is connected. New messages are pulled in and held for you to ingest."
        }
    }

    /// Ask for the wider `gmail.modify` grant label sync needs (motet#96).
    func reauthorize(
        _ source: SourceResponse, present: @MainActor (URL) async -> ConsentSheet.Result
    ) async -> Bool {
        guard let redirect = ConsentCallback.redirectURI(appDomain: appLinkDomain), canConsentHere else {
            consentNeedsWebApp = true
            return false
        }
        return await runConsent(what: "change labels in \(source.name)", present: present) { api in
            let started = try await api.reauthorizeSource(id: source.id, redirectURI: redirect)
            return (started.authorizationUrl, started.state)
        } finish: { api, code, state, _ in
            _ = try await api.completeSourceConsent(code: code, state: state)
            return "\(source.name) may now change labels on the messages you ingest."
        }
    }

    func syncNow(_ source: SourceResponse) async throws {
        let updated = try await api.pollSource(id: source.id)
        replace(updated)
    }

    func disconnect(_ source: SourceResponse) async throws {
        try await api.disconnectSource(id: source.id)
        await refresh()
    }

    func remove(_ source: SourceResponse) async throws {
        try await api.removeSource(id: source.id)
        await refresh()
    }

    func setLabelSync(_ source: SourceResponse, remove: String, add: String) async throws {
        let updated = try await api.setLabelSync(id: source.id, removeLabel: remove, addLabel: add)
        replace(updated)
    }

    private func replace(_ updated: SourceResponse) {
        guard var list = sources, let index = list.firstIndex(where: { $0.id == updated.id }) else { return }
        list[index] = updated
        sources = list
    }

    // MARK: - Connectors

    func createConnector(_ request: CreateConnectorRequest) async throws {
        let created = try await api.createConnector(request)
        connectors = (connectors ?? []) + [created]
    }

    func deleteConnector(_ connector: ConnectorResponse) async throws {
        try await api.deleteConnector(id: connector.id)
        connectors = connectors?.filter { $0.id != connector.id }
    }

    /// Authorize an MCP server: the third flow on `/oauth/callback`, finished at the
    /// connectors' own callback route with RFC 9207's issuer when the server sent one.
    func authorize(
        _ connector: ConnectorResponse, present: @MainActor (URL) async -> ConsentSheet.Result
    ) async -> Bool {
        guard let redirect = ConsentCallback.redirectURI(appDomain: appLinkDomain), canConsentHere else {
            consentNeedsWebApp = true
            return false
        }
        let authorized = await runConsent(what: connector.label, present: present) { api in
            let started = try await api.authorizeConnector(id: connector.id, redirectURI: redirect)
            return (started.authorizationUrl, started.state)
        } finish: { api, code, state, iss in
            let updated = try await api.completeConnectorConsent(code: code, state: state, iss: iss)
            return "\(updated.label) is authorized."
        }
        // A refused authorize writes its reason onto the row (a 409 for no registration, a
        // 502 for a server that would not answer), so the list is worth re-reading either way.
        await refresh()
        return authorized
    }

    // MARK: - Consent

    /// Start, present, and finish one consent. Every outcome ends in `notice`.
    private func runConsent(
        what: String,
        present: @MainActor (URL) async -> ConsentSheet.Result,
        start: (any SourcesAPI) async throws -> (url: String, state: String),
        finish: (any SourcesAPI, String, String, String?) async throws -> String
    ) async -> Bool {
        busy = true
        notice = nil
        consentNeedsWebApp = false
        defer { busy = false }
        let api = self.api
        do {
            let started = try await start(api)
            guard let url = URL(string: started.url) else {
                notice = "The API answered with a consent address that is not a URL."
                return false
            }
            switch await present(url) {
            case .dismissed:
                notice = "You closed the consent page before finishing. Nothing was changed."
                await refresh()
                return false
            case .refused:
                consentNeedsWebApp = true
                await refresh()
                return false
            case .callback(let callback):
                switch try ConsentCallback.outcome(
                    from: callback, appDomain: appLinkDomain, expectedState: started.state
                ) {
                case .denied(let error, let description):
                    notice = ConsentCallback.describeDenial(error: error, description: description, what: what)
                    await refresh()
                    return false
                case .granted(let code, let state, let iss):
                    notice = try await finish(api, code, state, iss)
                    await refresh()
                    return true
                }
            }
        } catch {
            notice = Self.describe(error)
            await refresh()
            return false
        }
    }

    private nonisolated static func catching<T: Sendable>(
        _ body: @Sendable () async throws -> T
    ) async -> Result<T, Error> {
        do { return .success(try await body()) } catch { return .failure(error) }
    }

    static func describe(_ error: Error) -> String {
        if let error = error as? MotetError {
            switch error {
            case .http(503, let detail?):
                return "This deployment can’t do that right now. That is configuration, not you: \(detail)"
            case .http(_, let detail?):
                return detail
            default:
                return error.description
            }
        }
        if let error = error as? ConsentCallback.Failure { return error.description }
        return error.localizedDescription
    }
}
