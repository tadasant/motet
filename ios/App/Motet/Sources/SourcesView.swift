import MotetKit
import SwiftUI

/// Where the content you trust comes from: the integrations catalog (motet#90) and the
/// connectors enrichment may use (motet#102), as the SPA's Sources and Credentials screens
/// have them — managed from the phone.
///
/// The thing this screen has to teach is the same one sentence the SPA's does: connected
/// means "pulled in and held", not "processed". Every count of held items says "waiting
/// for you".
struct SourcesView: View {
    @EnvironmentObject private var app: AppModel
    @StateObject private var holder = SourcesModelHolder()

    var body: some View {
        NavigationStack {
            Group {
                if let model = holder.model {
                    SourcesList()
                        .environmentObject(model)
                } else {
                    ProgressView()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(Theme.parchment)
                }
            }
            .navigationTitle("Sources")
            .navigationBarTitleDisplayMode(.inline)
        }
        .onAppear { holder.bind(app) }
    }
}

/// `@StateObject` needs its value at init, and the model needs the `AppModel` from the
/// environment, which only exists once the view is in the tree.
@MainActor
final class SourcesModelHolder: ObservableObject {
    @Published private(set) var model: SourcesModel?

    func bind(_ app: AppModel) {
        if model == nil { model = SourcesModel(app: app) }
    }
}

private struct SourcesList: View {
    @EnvironmentObject private var model: SourcesModel
    @State private var connecting = false
    @State private var addingConnector = false

    var body: some View {
        List {
            Section {
                Text("Connect a source and Motet pulls new items in on its own. **Nothing is processed until you ingest it.**")
                    .font(Theme.body(15, relativeTo: .subheadline))
                    .foregroundStyle(Theme.inkSoft)
                    .listRowBackground(Theme.parchment)
                if let notice = model.notice {
                    Text(notice)
                        .font(Theme.aside(15))
                        .foregroundStyle(Theme.ink)
                        .listRowBackground(Theme.surface)
                }
                if model.consentNeedsWebApp {
                    WebAppFallback(url: model.webSourcesURL)
                        .listRowBackground(Theme.surface)
                }
                if let error = model.loadError {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(Theme.body(14, relativeTo: .footnote))
                        .foregroundStyle(Theme.errorText)
                        .listRowBackground(Theme.parchment)
                }
            }

            if model.sources == nil {
                Section {
                    ProgressView().frame(maxWidth: .infinity).listRowBackground(Theme.parchment)
                }
            } else {
                gmailSection
                pasteSection
                comingSoonSection
                connectorSections
            }
        }
        .listStyle(.insetGrouped)
        .brandGround()
        .foregroundStyle(Theme.ink)
        .refreshable { await model.refresh() }
        .task { await model.refresh() }
        // Keep the counts honest while work is moving, as the SPA does: a panel saying
        // "3 processing" for ten minutes after they landed is a small lie.
        .task(id: model.inFlight) {
            while model.inFlight, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(10))
                await model.refresh()
            }
        }
        .sheet(isPresented: $connecting) { ConnectMailboxView().environmentObject(model) }
        .sheet(isPresented: $addingConnector) { AddConnectorView().environmentObject(model) }
    }

    // MARK: - Gmail

    private var gmailSection: some View {
        let rows = SourceStatus.ordered(model.rows(.gmail))
        let connected = rows.filter(\.connected).count
        return Section {
            ForEach(rows, id: \.id) { source in
                NavigationLink {
                    MailboxDetailView(sourceId: source.id).environmentObject(model)
                } label: {
                    SourceRow(source: source, counts: model.counts(for: source))
                }
                .listRowBackground(Theme.surface)
            }
            Button {
                connecting = true
            } label: {
                Label(
                    connected == 0 ? "Connect a mailbox" : "Connect another mailbox",
                    systemImage: "plus.circle"
                )
                .font(Theme.body(16, weight: 500))
            }
            .disabled(model.busy)
            .listRowBackground(Theme.surface)
        } header: {
            IntegrationHeader(integration: .gmail, card: SourceStatus.card(.gmail, rows: rows))
        } footer: {
            Text(Integration.gmail.detail)
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
    }

    // MARK: - Paste

    private var pasteSection: some View {
        let rows = model.rows(.paste)
        return Section {
            ForEach(rows, id: \.id) { source in
                SourceRow(source: source, counts: model.counts(for: source))
                    .listRowBackground(Theme.surface)
            }
        } header: {
            IntegrationHeader(integration: .paste, card: SourceStatus.card(.paste, rows: rows))
        } footer: {
            Text("\(Integration.paste.detail) Paste from the Backlog tab.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
    }

    private var comingSoonSection: some View {
        Section {
            ForEach([Integration.xBookmarks, .rss]) { integration in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(integration.name).font(Theme.body(16, weight: 500))
                        Spacer()
                        StatusPill(text: SourceStatus.Card.comingSoon.label, tone: .muted)
                    }
                    Text("\(integration.summary) \(integration.detail)")
                        .font(Theme.body(13, relativeTo: .footnote))
                        .foregroundStyle(Theme.inkSoft)
                }
                .listRowBackground(Theme.surface)
                .accessibilityElement(children: .combine)
            }
        } header: {
            Text("Coming soon").brandLabel()
        }
    }

    // MARK: - Connectors

    @ViewBuilder
    private var connectorSections: some View {
        let connectors = model.connectors ?? []
        let sites = connectors.filter { $0.kind == ConnectorStatus.siteKind }
        let servers = connectors.filter { $0.kind == ConnectorStatus.mcpKind }
        Section {
            if sites.isEmpty {
                Text("No sites yet. A newsletter linking to a site you add gets its full article fetched.")
                    .font(Theme.body(14, relativeTo: .footnote))
                    .foregroundStyle(Theme.inkSoft)
                    .listRowBackground(Theme.surface)
            }
            ForEach(sites, id: \.id) { connector in
                ConnectorRow(connector: connector).listRowBackground(Theme.surface)
            }
        } header: {
            Text("Sites").brandLabel()
        } footer: {
            Text("The sites Motet may fetch full articles from, when a newsletter is only a preview of one. Nothing is fetched until enrichment runs in this deployment.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
        Section {
            if servers.isEmpty {
                Text("No MCP servers.")
                    .font(Theme.body(14, relativeTo: .footnote))
                    .foregroundStyle(Theme.inkSoft)
                    .listRowBackground(Theme.surface)
            }
            ForEach(servers, id: \.id) { connector in
                ConnectorRow(connector: connector).listRowBackground(Theme.surface)
            }
            Button {
                addingConnector = true
            } label: {
                Label("Add a site or MCP server", systemImage: "plus.circle")
                    .font(Theme.body(16, weight: 500))
            }
            .listRowBackground(Theme.surface)
        } header: {
            Text("MCP servers").brandLabel()
        } footer: {
            Text("Tools the fetching agent may use — a read-only mailbox for a magic link, say.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
    }
}

/// The card's name and pill, as a section header.
private struct IntegrationHeader: View {
    let integration: Integration
    let card: SourceStatus.Card

    var body: some View {
        HStack {
            Text(integration.name).brandLabel()
            Spacer()
            StatusPill(text: card.label, tone: card == .error ? .attention : .plain)
                .textCase(nil)
        }
    }
}

/// One account behind an integration, in a line.
struct SourceRow: View {
    let source: SourceResponse
    let counts: SourceStatus.Counts

    var body: some View {
        let status = SourceStatus.row(source)
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(source.name).font(Theme.body(16, weight: 500))
                Spacer()
                StatusPill(text: status.label, tone: status == .error ? .attention : .plain)
            }
            Group {
                if status == .awaitingConsent {
                    Text("Consent not finished — nothing was connected.")
                } else if counts.held > 0 {
                    Text("\(counts.held) waiting for you · \(source.itemsPulledIn) pulled in")
                } else if SourceStatus.isPollable(source), let at = SourceStatus.lastSyncedAt(source) {
                    Text("Synced \(at.formatted(.relative(presentation: .named)))")
                } else {
                    Text("\(source.itemsPulledIn) pulled in, \(source.itemsIntegrated) ingested")
                }
            }
            .font(Theme.body(13, relativeTo: .footnote))
            .foregroundStyle(Theme.inkSoft)
        }
        .accessibilityElement(children: .combine)
    }
}

/// A status as the brand draws it: ink at opacity, and vermilion only for what is wrong —
/// the voice hues are never a status (brand/GUIDELINES.md).
struct StatusPill: View {
    enum Tone { case plain, muted, attention }
    let text: String
    var tone: Tone = .plain

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(tone == .attention ? Theme.error : Theme.ink.opacity(tone == .muted ? 0.3 : 0.66))
                .frame(width: 6, height: 6)
            Text(text)
        }
        .font(Theme.body(12, weight: 500, relativeTo: .caption))
        .foregroundStyle(tone == .attention ? Theme.errorText : Theme.inkSoft)
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .overlay(Capsule().strokeBorder(Theme.rule, lineWidth: 1))
    }
}

/// Said where a consent cannot finish on this phone, with the one place it can.
struct WebAppFallback: View {
    let url: URL?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("This phone can’t finish that consent itself: Google returns to the web app, and this build or this iOS can’t catch it on the way. Do it from the web app’s Sources screen instead.")
                .font(Theme.body(14, relativeTo: .footnote))
                .foregroundStyle(Theme.ink)
            if let url {
                Link(destination: url) {
                    Label("Open Sources in the web app", systemImage: "safari")
                        .font(Theme.body(15, weight: 600))
                }
            }
        }
    }
}
