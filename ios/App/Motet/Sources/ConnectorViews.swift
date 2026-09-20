import MotetKit
import SwiftUI

/// A site or MCP server enrichment may use (motet#102): what it is, whether it is ready,
/// and — for a server — authorizing it. Never a secret: `has_secret` is all the API says.
struct ConnectorRow: View {
    @EnvironmentObject private var model: SourcesModel
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    let connector: ConnectorResponse
    @State private var confirmingDelete = false
    @State private var error: String?

    var body: some View {
        let pill = ConnectorStatus.pill(connector)
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(connector.label).font(Theme.body(16, weight: 500))
                Spacer()
                StatusPill(text: pill.label, tone: pill == .error ? .attention : (pill == .needsAuth ? .muted : .plain))
            }
            Text(subtitle)
                .font(Theme.body(13, relativeTo: .footnote))
                .foregroundStyle(Theme.inkSoft)
                .lineLimit(2)
            if let lastError = connector.lastError, connector.status != "ready" {
                Text(lastError)
                    .font(Theme.body(13, relativeTo: .footnote))
                    .foregroundStyle(Theme.errorText)
            }
            if connector.kind == ConnectorStatus.mcpKind {
                Button(connector.status == "ready" ? "Re-authorize" : "Authorize") {
                    Task {
                        _ = await model.authorize(connector) { url in
                            await ConsentSheet.present(
                                url, appDomain: model.appLinkDomain, using: webAuthenticationSession
                            )
                        }
                    }
                }
                .font(Theme.body(15, weight: 600))
                .buttonStyle(.borderless)
                .disabled(model.busy)
            }
            if let error {
                Text(error).font(Theme.body(13, relativeTo: .footnote)).foregroundStyle(Theme.errorText)
            }
        }
        .swipeActions(edge: .trailing) {
            Button(role: .destructive) { confirmingDelete = true } label: {
                Label("Remove", systemImage: "trash")
            }
            .tint(Theme.error)
        }
        .contextMenu {
            Button(role: .destructive) { confirmingDelete = true } label: {
                Label("Remove", systemImage: "trash")
            }
        }
        .confirmationDialog(
            "Remove \(connector.label)?", isPresented: $confirmingDelete, titleVisibility: .visible
        ) {
            Button("Remove", role: .destructive) {
                Task {
                    do { try await model.deleteConnector(connector) } catch { self.error = SourcesModel.describe(error) }
                }
            }
        } message: {
            Text(connector.kind == ConnectorStatus.siteKind
                 ? "Nothing more is fetched from this site, and its saved login is forgotten."
                 : "The agent stops being handed this server, and its grant is forgotten.")
        }
    }

    private var subtitle: String {
        if connector.kind == ConnectorStatus.siteKind {
            let host = connector.domain ?? ""
            return connector.username.map { "\(host) · \($0)" } ?? host
        }
        let url = connector.url ?? ""
        return connector.domains.isEmpty ? url : "\(url) · only for \(connector.domains.joined(separator: ", "))"
    }
}

/// Add a site or an MCP server — the SPA's `AddConnector`, with the risk said at the moment
/// a server is added rather than buried. The API refuses an MCP row without the
/// acknowledgement too, so the toggle is the explanation and the API is the control.
struct AddConnectorView: View {
    @EnvironmentObject private var model: SourcesModel
    @Environment(\.dismiss) private var dismiss

    enum Kind: String, CaseIterable, Identifiable {
        case site, mcp
        var id: String { rawValue }
        var title: String { self == .site ? "Site" : "MCP server" }
    }

    @State private var kind: Kind = .site
    @State private var domain = ""
    @State private var username = ""
    @State private var password = ""
    @State private var url = ""
    @State private var domains = ""
    @State private var acknowledged = false
    @State private var label = ""
    @State private var saving = false
    @State private var error: String?

    private var normalizedDomain: String { ConnectorStatus.normalizeDomain(domain) }

    private var canAdd: Bool {
        switch kind {
        case .site:
            let hasPasswordWithoutUser = !password.isEmpty && username.trimmingCharacters(in: .whitespaces).isEmpty
            return ConnectorStatus.looksLikeDomain(normalizedDomain) && !hasPasswordWithoutUser
        case .mcp:
            return url.trimmingCharacters(in: .whitespaces).hasPrefix("https://") && acknowledged
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("Kind", selection: $kind) {
                        ForEach(Kind.allCases) { Text($0.title).tag($0) }
                    }
                    .pickerStyle(.segmented)
                }
                .listRowBackground(Theme.parchment)

                switch kind {
                case .site: siteFields
                case .mcp: serverFields
                }

                Section {
                    TextField(kind == .site ? "Example News" : "Mail (read-only)", text: $label)
                } header: {
                    Text("Label (optional)").brandLabel()
                }
                .listRowBackground(Theme.surface)

                if let error {
                    Section {
                        Text(error).foregroundStyle(Theme.errorText)
                    }
                    .listRowBackground(Theme.parchment)
                }
            }
            .brandGround()
            .foregroundStyle(Theme.ink)
            .font(Theme.body(16))
            .navigationTitle("Add a connector")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Adding…" : "Add") { Task { await add() } }
                        .disabled(!canAdd || saving)
                }
            }
        }
        .preferredColorScheme(.light)
        .tint(Theme.ink)
    }

    @ViewBuilder
    private var siteFields: some View {
        Section {
            TextField("example.com", text: $domain)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .onSubmit { domain = normalizedDomain }
        } header: {
            Text("Domain").brandLabel()
        } footer: {
            Text("Newsletter links to this site and its subdomains get the full article fetched. Nothing else is.")
                .font(Theme.aside(14)).foregroundStyle(Theme.inkSoft)
        }
        .listRowBackground(Theme.surface)
        Section {
            TextField("Username or email", text: $username)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .textContentType(.username)
            SecureField("Password", text: $password)
                .textContentType(.password)
        } header: {
            Text("Login (optional)").brandLabel()
        } footer: {
            Text("Leave both blank for a site the newsletter’s own link opens; leave the password blank for one that emails a code. The password is sealed and never shown again.")
                .font(Theme.aside(14)).foregroundStyle(Theme.inkSoft)
        }
        .listRowBackground(Theme.surface)
    }

    @ViewBuilder
    private var serverFields: some View {
        Section {
            TextField("https://example.com/mcp", text: $url)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            TextField("Only for these sites (optional)", text: $domains)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        } header: {
            Text("Server").brandLabel()
        } footer: {
            Text("OAuth only. Press Authorize after adding. Leave the sites blank to hand this server to the agent for every site you added.")
                .font(Theme.aside(14)).foregroundStyle(Theme.inkSoft)
        }
        .listRowBackground(Theme.surface)
        Section {
            Text("The enrichment agent that is handed this server also reads web pages nobody at Motet wrote. A hostile page can instruct it to use this server with your account — to read what the server can see and carry it somewhere else.")
            Text("Enrichment narrows that — its browser is locked to the article’s site, it holds no database or keys, and its transcript keeps no result from a server like this one — but it does not close it. Connect only a server whose worst case you would accept: a read-only mailbox, not one that can send.")
            Toggle("I understand that a web page the agent reads can steer it into using this server with my account.", isOn: $acknowledged)
        } header: {
            Text("Before you connect a server").brandLabel()
        }
        .font(Theme.body(14, relativeTo: .footnote))
        .listRowBackground(Theme.surface)
    }

    private func add() async {
        saving = true
        error = nil
        defer { saving = false }
        let trimmedLabel = label.trimmingCharacters(in: .whitespacesAndNewlines)
        let request: CreateConnectorRequest
        switch kind {
        case .site:
            let user = username.trimmingCharacters(in: .whitespacesAndNewlines)
            request = CreateConnectorRequest(
                acknowledgeRisk: false,
                domain: normalizedDomain,
                kind: ConnectorStatus.siteKind,
                label: trimmedLabel.isEmpty ? nil : trimmedLabel,
                password: password.isEmpty ? nil : password,
                username: user.isEmpty ? nil : user
            )
        case .mcp:
            request = CreateConnectorRequest(
                acknowledgeRisk: acknowledged,
                domains: ConnectorStatus.domainList(domains),
                kind: ConnectorStatus.mcpKind,
                label: trimmedLabel.isEmpty ? nil : trimmedLabel,
                url: url.trimmingCharacters(in: .whitespacesAndNewlines)
            )
        }
        do {
            try await model.createConnector(request)
            password = ""
            dismiss()
        } catch {
            self.error = SourcesModel.describe(error)
        }
    }
}
