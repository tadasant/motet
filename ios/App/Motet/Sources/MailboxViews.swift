import MotetKit
import SwiftUI

/// One mailbox in full: what it is, when it last synced and what that found, what it has
/// pulled in and where that went, and the things you can do to it — the SPA's
/// `SourceDetail`, on the phone.
struct MailboxDetailView: View {
    @EnvironmentObject private var model: SourcesModel
    @Environment(\.dismiss) private var dismiss
    let sourceId: String

    @State private var sync: SyncState = .idle
    @State private var confirmingDisconnect = false
    @State private var actionError: String?
    @State private var working = false

    /// "Sync now", in words that do not promise more than the queue does.
    enum SyncState: Equatable {
        case idle
        /// The poll is enqueued; `before` is the `last_sync.at` it has to move past.
        case queued(before: Date?, startedAt: Date)
        case done(at: Date, queued: Int)
        case slow
        case failed(String)
    }

    /// How long "Sync now" watches for the poll to land, and how often it looks.
    private static let syncWatchSeconds: TimeInterval = 120
    private static let syncPoll: Duration = .seconds(2)

    var body: some View {
        Group {
            if let source = model.source(id: sourceId) {
                content(source)
            } else {
                // Removed, or gone from the list: nothing to show and nothing to act on.
                Text("This source is no longer there.")
                    .font(Theme.aside(16))
                    .foregroundStyle(Theme.inkSoft)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Theme.parchment)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
    }

    private func content(_ source: SourceResponse) -> some View {
        let status = SourceStatus.row(source)
        let counts = model.counts(for: source)
        return List {
            Section {
                HStack {
                    Text(source.name).font(Theme.display(22, relativeTo: .title3))
                    Spacer()
                    StatusPill(text: status.label, tone: status == .error ? .attention : .plain)
                }
                .listRowBackground(Theme.parchment)
                notices(source, status: status)
            }

            Section {
                facts(source, status: status)
            } header: {
                Text("About").brandLabel()
            }
            .listRowBackground(Theme.surface)

            if status != .awaitingConsent {
                Section {
                    HStack(spacing: 8) {
                        Stat(label: "Waiting for you", value: counts.held, emphasis: counts.held > 0)
                        Stat(label: "Processing", value: counts.processing)
                        Stat(label: "Failed", value: counts.failed, bad: counts.failed > 0)
                        Stat(label: "Landed recently", value: counts.integrated)
                    }
                    .listRowBackground(Theme.parchment)
                    if counts.held > 0 {
                        Text("**\(counts.held) item\(counts.held == 1 ? "" : "s") waiting** — pulled in and not yet processed. Ingest them from the Backlog.")
                            .font(Theme.body(14, relativeTo: .footnote))
                            .listRowBackground(Theme.surface)
                    }
                } header: {
                    Text("Pulled in").brandLabel()
                }
            }

            if SourceStatus.isPollable(source), status != .awaitingConsent {
                Section {
                    Button(isSyncing ? "Syncing…" : "Sync now") {
                        Task { await syncNow(source) }
                    }
                    .font(Theme.body(16, weight: 600))
                    .disabled(isSyncing || !source.connected || !source.active)
                    if let line = syncLine {
                        Text(line.text)
                            .font(Theme.body(14, relativeTo: .footnote))
                            .foregroundStyle(line.isError ? Theme.errorText : Theme.inkSoft)
                    } else if !source.connected {
                        Text("No credential to sync with.").font(Theme.body(14, relativeTo: .footnote)).foregroundStyle(Theme.inkSoft)
                    } else if !source.active {
                        Text("Paused: not polled.").font(Theme.body(14, relativeTo: .footnote)).foregroundStyle(Theme.inkSoft)
                    }
                }
                .listRowBackground(Theme.surface)
            }

            if SourceStatus.isPollable(source), source.connected, let labels = source.labelSync {
                LabelSyncSection(source: source, labels: labels)
            }

            Section {
                if status == .awaitingConsent {
                    Button("Remove this attempt", role: .destructive) {
                        Task {
                            // Gone from the list once removed; there is nothing left to show here.
                            if await run({ try await model.remove(source) }) { dismiss() }
                        }
                    }
                    .disabled(working)
                }
                if SourceStatus.isPollable(source), source.connected {
                    Button("Disconnect", role: .destructive) { confirmingDisconnect = true }
                        .disabled(working)
                }
                if let actionError {
                    Text(actionError)
                        .font(Theme.body(14, relativeTo: .footnote))
                        .foregroundStyle(Theme.errorText)
                }
            }
            .listRowBackground(Theme.surface)
        }
        .listStyle(.insetGrouped)
        .brandGround()
        .foregroundStyle(Theme.ink)
        .refreshable { await model.refresh() }
        .confirmationDialog(
            "Disconnect this mailbox?", isPresented: $confirmingDisconnect, titleVisibility: .visible
        ) {
            Button("Disconnect", role: .destructive) {
                Task { await run { try await model.disconnect(source) } }
            }
        } message: {
            Text("It stops being polled and its credential is forgotten; what it pulled in stays, because episodes already cite it.")
        }
        .task(id: syncWatchKey) { await watchSync() }
        .onChange(of: SourceStatus.syncedAt(source)) { _, syncedAt in
            settleSync(source, syncedAt: syncedAt)
        }
    }

    // MARK: - Pieces

    @ViewBuilder
    private func notices(_ source: SourceResponse, status: SourceStatus.Row) -> some View {
        if status == .awaitingConsent {
            Text("This row was created when Connect was pressed and no credential ever arrived — you cancelled on Google’s page, or closed it before finishing. Nothing was connected and nothing was changed. Connect again, or remove it: it is not polled.")
                .font(Theme.body(14, relativeTo: .footnote))
                .listRowBackground(Theme.surface)
        }
        if status == .disconnected {
            Text("Disconnected\(source.disconnectedAt.map { " " + $0.formatted(.relative(presentation: .named)) } ?? ""). The credential is forgotten and the mailbox is no longer polled; everything it pulled in stays.")
                .font(Theme.body(14, relativeTo: .footnote))
                .listRowBackground(Theme.surface)
        }
        if let error = source.lastError {
            VStack(alignment: .leading, spacing: 4) {
                Text("The last sync failed:").font(Theme.body(14, weight: 600, relativeTo: .footnote))
                Text(error).font(Theme.body(14, relativeTo: .footnote))
            }
            .foregroundStyle(Theme.errorText)
            .listRowBackground(Theme.surface)
        }
    }

    @ViewBuilder
    private func facts(_ source: SourceResponse, status: SourceStatus.Row) -> some View {
        Fact(status == .awaitingConsent ? "Started" : "Added",
             source.createdAt.formatted(date: .abbreviated, time: .omitted))
        if SourceStatus.isPollable(source) {
            Fact("Last sync", SourceStatus.lastSyncedAt(source)
                .map { $0.formatted(.relative(presentation: .named)) } ?? "Never polled")
            Fact("Last result", SourceStatus.describeLastSync(source) ?? "No sync has completed yet.",
                 isError: source.lastSync?.error != nil)
            if let query = source.query {
                Fact("Filter", query == SourceStatus.defaultQuery ? "\(query) (the default)" : query)
            }
            if let days = source.firstSyncDays {
                Fact("Sync window", "The first sync reached back \(days) day\(days == 1 ? "" : "s"); older mail was not pulled in.")
            }
        }
        if status != .awaitingConsent {
            Fact("All time", "\(source.itemsPulledIn) pulled in, \(source.itemsIntegrated) ingested")
        }
        if !source.scopes.isEmpty {
            Fact("Access", source.scopes.map(SourceStatus.describeScope).joined(separator: ", "))
        }
    }

    // MARK: - Sync now

    private var isSyncing: Bool {
        if case .queued = sync { return true }
        return false
    }

    /// Restarts the watch whenever a new sync is queued.
    private var syncWatchKey: Date? {
        if case .queued(_, let startedAt) = sync { return startedAt }
        return nil
    }

    private var syncLine: (text: String, isError: Bool)? {
        switch sync {
        case .idle: return nil
        case .queued: return (SourceStatus.describeQueuedSync(model.worker), false)
        case .done(let at, let queued):
            return ("Synced \(at.formatted(.relative(presentation: .named))). \(queued > 0 ? "New items are held for you to ingest." : "Nothing new.")", false)
        case .slow:
            return ("Still queued after two minutes. It runs when a worker gets to it; Last sync updates when it does.", false)
        case .failed(let message): return (message, true)
        }
    }

    private func syncNow(_ source: SourceResponse) async {
        sync = .queued(before: SourceStatus.syncedAt(source), startedAt: .now)
        do {
            try await model.syncNow(source)
        } catch {
            sync = .failed(SourcesModel.describe(error))
        }
    }

    /// The poll route enqueues and answers at once; the sync has *run* when the row's
    /// `last_sync.at` moves. So re-fetch on an interval until it does, and stop watching —
    /// not the sync — after a bound.
    private func watchSync() async {
        guard case .queued(_, let startedAt) = sync else { return }
        while !Task.isCancelled, case .queued = sync {
            try? await Task.sleep(for: Self.syncPoll)
            if Date.now.timeIntervalSince(startedAt) > Self.syncWatchSeconds {
                sync = .slow
                return
            }
            await model.refresh()
        }
    }

    /// A poll that gave up records its error on `last_sync` and moves its time too, so a
    /// moved time is "it ran", not "it worked".
    private func settleSync(_ source: SourceResponse, syncedAt: Date?) {
        guard case .queued(let before, _) = sync, let syncedAt, syncedAt != before else { return }
        if let error = source.lastSync?.error {
            sync = .failed("The sync gave up: \(error)")
        } else {
            sync = .done(at: syncedAt, queued: source.lastSync?.queued ?? 0)
        }
    }

    @discardableResult
    private func run(_ action: () async throws -> Void) async -> Bool {
        working = true
        actionError = nil
        defer { working = false }
        do {
            try await action()
            return true
        } catch {
            actionError = SourcesModel.describe(error)
            return false
        }
    }
}

/// Label sync (motet#96): settings, and the one consent that asks for more than read-only
/// access. Setting labels widens nothing; re-authorizing is what asks Google for
/// `gmail.modify`, and only once labels are set.
private struct LabelSyncSection: View {
    @EnvironmentObject private var model: SourcesModel
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    let source: SourceResponse
    let labels: LabelSyncResponse

    @State private var remove = ""
    @State private var add = ""
    @State private var saving = false
    @State private var error: String?

    private var changed: Bool {
        remove.trimmingCharacters(in: .whitespaces) != (labels.removeLabel ?? "")
            || add.trimmingCharacters(in: .whitespaces) != (labels.addLabel ?? "")
    }

    var body: some View {
        Section {
            labelPicker("Remove", selection: $remove)
            labelPicker("Add", selection: $add)
            if changed {
                Button(saving ? "Saving…" : "Save labels") { Task { await save() } }
                    .disabled(saving)
            }
            statusLine
            if labels.status == "needs_reauthorization" {
                Button("Allow Motet to change labels") {
                    Task {
                        _ = await model.reauthorize(source) { url in
                            await ConsentSheet.present(
                                url, appDomain: model.appLinkDomain, using: webAuthenticationSession
                            )
                        }
                    }
                }
                .font(Theme.body(16, weight: 600))
                .disabled(model.busy)
            }
            if let error {
                Text(error).font(Theme.body(14, relativeTo: .footnote)).foregroundStyle(Theme.errorText)
            }
        } header: {
            Text("Label sync").brandLabel()
        } footer: {
            Text("When you ingest an item from this mailbox, Motet can move its message — say from Newsletters to Completed. Connecting asked for read-only access; changing labels needs one more consent.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
        .listRowBackground(Theme.surface)
        .onAppear {
            remove = labels.removeLabel ?? ""
            add = labels.addLabel ?? ""
        }
        // Only a change to what is *stored* resets the fields. A refresh moves the catalog,
        // the counts and the timestamps too — every two seconds while Sync now watches — and
        // resetting on those would wipe a choice mid-edit.
        .onChange(of: [labels.removeLabel ?? "", labels.addLabel ?? ""]) { _, stored in
            remove = stored[0]
            add = stored[1]
        }
    }

    /// A label name, typed or picked from the ones the last poll cached. Typed as well as
    /// picked, as on the web: before the first poll there is no list, and the API resolves a
    /// name it has not cached by asking Gmail.
    private func labelPicker(_ title: String, selection: Binding<String>) -> some View {
        HStack {
            Text(title)
            TextField("None", text: selection)
                .multilineTextAlignment(.trailing)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
            if !labels.availableLabels.isEmpty {
                Menu {
                    Button("None") { selection.wrappedValue = "" }
                    ForEach(labels.availableLabels, id: \.self) { name in
                        Button(name) { selection.wrappedValue = name }
                    }
                } label: {
                    Image(systemName: "chevron.up.chevron.down")
                        .foregroundStyle(Theme.inkSoft)
                }
                .accessibilityLabel("\(title): choose a label")
            }
        }
        .font(Theme.body(16))
    }

    @ViewBuilder
    private var statusLine: some View {
        let move = SourceStatus.describeLabelMove(remove: labels.removeLabel, add: labels.addLabel)
        switch labels.status {
        case "on":
            Text("On — ingesting an item \(move ?? "changes its labels").")
                .font(Theme.body(14, relativeTo: .footnote))
        case "needs_reauthorization":
            Text("Labels are set, but this mailbox was connected read-only, so nothing is changed until you allow it.")
                .font(Theme.body(14, relativeTo: .footnote))
        default:
            Text(labels.availableLabels.isEmpty
                 ? "Off. Type a label to add or remove; the list of yours arrives with the next sync."
                 : "Off. Choose a label to add or remove.")
                .font(Theme.body(14, relativeTo: .footnote))
                .foregroundStyle(Theme.inkSoft)
        }
        if labels.status != "off", labels.failedItems > 0 {
            Text("\(labels.failedItems) ingested item\(labels.failedItems == 1 ? "" : "s") could not be moved.\(labels.lastError.map { " \($0)" } ?? "")")
                .font(Theme.body(14, relativeTo: .footnote))
                .foregroundStyle(Theme.errorText)
        }
    }

    private func save() async {
        saving = true
        error = nil
        defer { saving = false }
        do {
            try await model.setLabelSync(source, remove: remove, add: add)
        } catch {
            self.error = SourcesModel.describe(error)
        }
    }
}

/// A fact about a source: a label and its value, stacked so a long filter wraps.
private struct Fact: View {
    let name: String
    let value: String
    var isError = false

    init(_ name: String, _ value: String, isError: Bool = false) {
        self.name = name
        self.value = value
        self.isError = isError
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(name).brandLabel(size: 11)
            Text(value)
                .font(Theme.body(15, relativeTo: .subheadline))
                .foregroundStyle(isError ? Theme.errorText : Theme.ink)
        }
        .accessibilityElement(children: .combine)
    }
}

/// One count tile.
private struct Stat: View {
    let label: String
    let value: Int
    var emphasis = false
    var bad = false

    var body: some View {
        VStack(spacing: 2) {
            Text("\(value)")
                .font(Theme.display(22, relativeTo: .title3))
                .foregroundStyle(bad ? Theme.errorText : Theme.ink.opacity(value == 0 ? 0.42 : 1))
                .monospacedDigit()
            Text(label)
                .font(Theme.body(11, weight: 500, relativeTo: .caption2))
                .foregroundStyle(Theme.inkSoft)
                .multilineTextAlignment(.center)
                .lineLimit(2)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .brandCard(fill: emphasis ? Theme.surface : Theme.parchment)
        .accessibilityElement(children: .combine)
    }
}

/// The connect flow's first half, presented: an explainer, two fields, one button.
struct ConnectMailboxView: View {
    @EnvironmentObject private var model: SourcesModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    @State private var name = "Gmail"
    @State private var query = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("We’ll read newsletters matching your filter — read-only, and only the messages the search matches. What arrives is pulled in and held. **Nothing is processed until you choose to ingest it.**")
                        .font(Theme.body(15, relativeTo: .subheadline))
                    Text("Motet stores only a refresh token, sealed: nothing in the API can read it back. Google asks for consent on its own page and hands you back here.")
                        .font(Theme.aside(14))
                        .foregroundStyle(Theme.inkSoft)
                }
                .listRowBackground(Theme.parchment)

                Section {
                    TextField("Name", text: $name)
                    TextField(SourceStatus.defaultQuery, text: $query)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Name and Gmail search").brandLabel()
                } footer: {
                    Text("Which messages count as newsletters, in Gmail’s own search syntax. Left blank it is the default above, which needs no setup.")
                        .font(Theme.aside(14))
                        .foregroundStyle(Theme.inkSoft)
                }
                .listRowBackground(Theme.surface)

                Section {
                    if model.canConsentHere {
                        Button(model.busy ? "Waiting for Google…" : "Connect Gmail") {
                            Task { await connect() }
                        }
                        .buttonStyle(PrimaryButtonStyle())
                        .disabled(model.busy || name.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                    if !model.canConsentHere || model.consentNeedsWebApp {
                        WebAppFallback(url: model.webSourcesURL)
                    }
                    if let notice = model.notice, !model.busy {
                        Text(notice).font(Theme.aside(15))
                    }
                }
                .listRowBackground(Theme.parchment)
            }
            .brandGround()
            .foregroundStyle(Theme.ink)
            .font(Theme.body(16))
            .navigationTitle("Connect a mailbox")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
        .preferredColorScheme(.light)
        .tint(Theme.ink)
    }

    private func connect() async {
        let connected = await model.connectMailbox(name: name, query: query) { url in
            await ConsentSheet.present(url, appDomain: model.appLinkDomain, using: webAuthenticationSession)
        }
        if connected { dismiss() }
    }
}
