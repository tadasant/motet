import MotetKit
import SwiftUI

/// The visual half of read state.
///
/// Invariant 5: this list and the audio write the same fact. Marking something read here is
/// the same column that listening past it sets, and the row can be put *back* — the backlog
/// is the product's memory, and being unable to undo is worse than never having marked it.
struct BacklogView: View {
    @EnvironmentObject private var model: AppModel
    /// The held list's own model. The Backlog tab has no `SourcesModel` — that one is scoped
    /// to the Sources tab — and "what has been pulled in and not yet ingested" is a Backlog
    /// question: the Backlog is where a person looks for mail that has arrived (motet#139).
    @StateObject private var heldHolder = SourcesModelHolder()
    @State private var showingRead = false
    @State private var isPasting = false
    /// Picking stories for an episode made of just those. Off, a row is a row; on, a tap
    /// ticks it and the bar at the bottom makes the episode.
    @State private var isSelecting = false
    @State private var selection: Set<String> = []
    @State private var isGenerating = false
    /// Said once, under the list, after an episode was queued from a pick.
    @State private var queuedMessage: String?
    /// What the sheet's keep-in-backlog switch starts at. Off: see `PickedEpisodeView`.
    @State private var keepInBacklogByDefault = false

    init() {}

    #if DEBUG
    init(fixture: ScreenshotFixture) {
        _isSelecting = State(initialValue: fixture.isSelecting)
        _selection = State(initialValue: fixture.selection)
        _isGenerating = State(initialValue: fixture.isGenerating)
        _keepInBacklogByDefault = State(initialValue: fixture.keepInBacklog)
    }
    #endif

    private var visibleItems: [NewsItemResponse] {
        showingRead ? model.newsItems : model.newsItems.filter { !$0.read }
    }

    /// The picks, oldest first — the order the server will speak them in, and so the order
    /// the duration cap drops from the end of. The backlog itself lists newest first.
    private var pickedItems: [NewsItemResponse] {
        model.newsItems
            .filter { selection.contains($0.id) }
            .sorted { ($0.createdAt, $0.id) < ($1.createdAt, $1.id) }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ConnectionBanner(message: model.connectionMessage)
                List {
                    if let held = heldHolder.model, !held.held.isEmpty, !isSelecting {
                        heldBanner(held)
                    }
                    if visibleItems.isEmpty {
                        ContentUnavailableView {
                            VStack(spacing: 12) {
                                VoiceDots(size: 8)
                                Text(showingRead ? "Nothing here" : "All caught up")
                                    .font(Theme.display(26, relativeTo: .title2))
                                    .foregroundStyle(Theme.ink)
                            }
                        } description: {
                            Text("Paste in a newsletter or a thread to start a backlog.")
                                .font(Theme.body(15, relativeTo: .subheadline))
                                .foregroundStyle(Theme.inkSoft)
                        }
                        .listRowBackground(Theme.parchment)
                        .listRowSeparator(.hidden)
                    }
                    ForEach(visibleItems, id: \.id) { item in
                        rowLink(item)
                            .padding(.vertical, 6)
                            .listRowBackground(Theme.parchment)
                            .listRowSeparatorTint(Theme.rule)
                            .swipeActions(edge: .leading) {
                                if !isSelecting {
                                    Button {
                                        Task { await model.setRead(!item.read, newsItem: item) }
                                    } label: {
                                        Label(
                                            item.read ? "Unread" : "Read",
                                            systemImage: item.read ? "envelope.badge" : "envelope.open"
                                        )
                                    }
                                    // Read state is a status, so it is ink, never a voice hue.
                                    .tint(Theme.ink)
                                }
                            }
                    }
                }
                .listStyle(.plain)
                .brandGround()
                .refreshable {
                    queuedMessage = nil
                    await model.refresh()
                }
                if isSelecting {
                    generateBar
                } else if let queuedMessage {
                    Label(queuedMessage, systemImage: "waveform")
                        .font(Theme.body(13, weight: 500, relativeTo: .footnote))
                        .foregroundStyle(Theme.inkSoft)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal)
                        .padding(.vertical, 10)
                        .background(Theme.surface)
                }
            }
            .background(Theme.parchment)
            .navigationTitle(isSelecting ? selectionTitle : "Backlog")
            .toolbar { toolbar }
            .task {
                // Bound and refreshed in one place: `.onAppear` and `.task` have no
                // guaranteed order between them, so binding in the first and reading the
                // model in the second could read a `nil` that is filled a tick later.
                heldHolder.bind(model)
                await heldHolder.model?.refresh()
            }
            .sheet(isPresented: $isPasting) { PasteView() }
            .sheet(isPresented: $isGenerating) {
                PickedEpisodeView(items: pickedItems, keepInBacklog: keepInBacklogByDefault) {
                    selection.removeAll()
                    isSelecting = false
                    queuedMessage = "Episode queued — it will appear under Episodes."
                }
            }
        }
    }

    private var selectionTitle: String {
        selection.isEmpty ? "Pick stories" : "\(selection.count) picked"
    }

    /// What has been pulled in and is waiting for a person, above the stories that already
    /// went through dedup.
    ///
    /// Above rather than below for the SPA's reason: "where did the mail I just synced go"
    /// is asked immediately, and an answer under a long list of older stories is an answer
    /// nobody scrolls to. Before this, the phone answered it nowhere at all — the list here
    /// is news items, and a held item is deliberately not one yet.
    @ViewBuilder
    private func heldBanner(_ sources: SourcesModel) -> some View {
        NavigationLink {
            HeldItemsView().environmentObject(sources)
        } label: {
            VStack(alignment: .leading, spacing: 4) {
                Text("\(sources.held.count) pulled in, waiting for you")
                    .font(Theme.body(16, weight: 600))
                Text("Nothing here has cost inference yet — pick what you want.")
                    .font(Theme.body(13, relativeTo: .footnote))
                    .foregroundStyle(Theme.inkSoft)
            }
            .padding(.vertical, 4)
        }
        .listRowBackground(Theme.surface)
    }

    /// A row is named after its source, not after dedup.
    ///
    /// One source: that newsletter's own subject line, verbatim, because it is what its
    /// reader recognises. Several: dedup's title, which is the only one that can name more
    /// than one write-up at once, with a count saying how many. The server decides which
    /// (`display_title`); this row renders it. Tapping opens the provenance — the summary
    /// lives there now rather than under every row.
    @ViewBuilder
    private func row(_ item: NewsItemResponse) -> some View {
        let isPicked = selection.contains(item.id)
        HStack(alignment: .top, spacing: 12) {
            if isSelecting {
                Image(systemName: isPicked ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 22))
                    .foregroundStyle(isPicked ? Theme.ink : Theme.inkMute)
                    .padding(.top, 2)
                    .accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(item.listTitle)
                    .font(Theme.display(18, relativeTo: .headline))
                    .foregroundStyle(item.read ? Theme.inkSoft : Theme.ink)
                if isSelecting {
                    // Picking is the one place the summary still earns its line: the title
                    // alone is not always enough to decide what goes in an episode.
                    Text(item.summary)
                        .font(Theme.body(15, relativeTo: .subheadline))
                        .foregroundStyle(Theme.inkSoft)
                        .lineLimit(2)
                }
                if item.sourceCount > 1 {
                    // The affordance for a merge: how many write-ups are behind this one
                    // line. One source needs none — the line *is* that source's title.
                    Text("\(item.sourceCount) sources")
                        .brandLabel(size: 11)
                }
            }
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isPicked ? .isSelected : [])
    }

    /// Selecting turns the whole row into a checkbox; otherwise it opens the provenance.
    @ViewBuilder
    private func rowLink(_ item: NewsItemResponse) -> some View {
        if isSelecting {
            row(item)
                .accessibilityAddTraits(.isButton)
                .onTapGesture {
                    if selection.contains(item.id) {
                        selection.remove(item.id)
                    } else {
                        selection.insert(item.id)
                    }
                }
        } else {
            NavigationLink { NewsItemDetailView(item: item) } label: { row(item) }
        }
    }

    private var generateBar: some View {
        VStack(spacing: 0) {
            Rectangle().fill(Theme.rule).frame(height: 1)
            Button {
                isGenerating = true
            } label: {
                Text(selection.isEmpty ? "Generate episode" : "Generate episode (\(selection.count))")
            }
            .buttonStyle(PrimaryButtonStyle())
            .disabled(selection.isEmpty)
            .padding(.horizontal)
            .padding(.vertical, 12)
        }
        .background(Theme.parchment)
    }

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        if isSelecting {
            ToolbarItem(placement: .cancellationAction) {
                Button("Cancel") {
                    selection.removeAll()
                    isSelecting = false
                }
            }
            ToolbarItem(placement: .primaryAction) {
                let allVisible = Set(visibleItems.map(\.id))
                Button(selection.isSuperset(of: allVisible) && !allVisible.isEmpty ? "None" : "All") {
                    if selection.isSuperset(of: allVisible) {
                        selection.subtract(allVisible)
                    } else {
                        selection.formUnion(allVisible)
                    }
                }
                .disabled(allVisible.isEmpty)
            }
        } else {
            ToolbarItem(placement: .primaryAction) {
                Button { isPasting = true } label: {
                    Label("Paste in", systemImage: "doc.on.clipboard")
                }
            }
            ToolbarItem(placement: .primaryAction) {
                // An icon, so the toolbar keeps room for the "Show read" pill beside it.
                Button {
                    queuedMessage = nil
                    isSelecting = true
                } label: {
                    Label("Select", systemImage: "checkmark.circle")
                }
                .disabled(visibleItems.isEmpty)
            }
            ToolbarItem(placement: .topBarLeading) {
                Toggle("Show read", isOn: $showingRead)
                    .toggleStyle(PillToggleStyle())
                    .fixedSize()
            }
        }
    }
}

/// An episode made of just the picked stories.
///
/// Keeping them in the backlog is off by default, because that is what every other episode
/// does: a story you listen past is read (invariant 5). On, listening leaves them unread —
/// for hearing a few now without losing them from the list.
struct PickedEpisodeView: View {
    let items: [NewsItemResponse]
    let onQueued: () -> Void

    init(
        items: [NewsItemResponse], keepInBacklog: Bool = false, onQueued: @escaping () -> Void
    ) {
        self.items = items
        self.onQueued = onQueued
        _keepInBacklog = State(initialValue: keepInBacklog)
    }

    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    /// Empty, and sent as nothing: the server names an episode after the day it was made.
    @State private var title = ""
    @State private var minutes = 30
    @State private var keepInBacklog: Bool
    @State private var isSending = false
    @State private var failure: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Title", text: $title, prompt: Text("Today's date"))
                        .font(Theme.body(16))
                    Stepper("Up to \(minutes) minutes", value: $minutes, in: 5...90, step: 5)
                        .font(Theme.body(16))
                        .monospacedDigit()
                } footer: {
                    Text("Spoken oldest first; what doesn't fit is left out.")
                        .font(Theme.body(13, relativeTo: .footnote))
                        .foregroundStyle(Theme.inkSoft)
                }
                .listRowBackground(Theme.surface)
                Section {
                    Toggle("Keep in backlog", isOn: $keepInBacklog)
                        .font(Theme.body(16))
                        .tint(Theme.ink)
                } footer: {
                    Text(
                        keepInBacklog
                            ? "These stories stay in your backlog."
                            : "Each story is marked read once you've heard it."
                    )
                    .font(Theme.body(13, relativeTo: .footnote))
                    .foregroundStyle(Theme.inkSoft)
                }
                .listRowBackground(Theme.surface)
                Section {
                    ForEach(items, id: \.id) { item in
                        Text(item.title)
                            .font(Theme.body(15, relativeTo: .subheadline))
                            .foregroundStyle(Theme.ink)
                    }
                } header: {
                    Text("\(items.count) \(items.count == 1 ? "story" : "stories")").brandLabel()
                }
                .listRowBackground(Theme.surface)
                if let failure {
                    Section {
                        Text(failure)
                            .font(Theme.body(14, relativeTo: .footnote))
                            .foregroundStyle(Theme.errorText)
                    }
                    .listRowBackground(Theme.surface)
                }
            }
            .brandGround()
            .foregroundStyle(Theme.ink)
            .navigationTitle("New episode")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(isSending ? "Making…" : "Make it") {
                        Task { await make() }
                    }
                    .disabled(isSending || items.isEmpty)
                }
            }
        }
        .preferredColorScheme(.light)
        .tint(Theme.ink)
    }

    private func make() async {
        isSending = true
        failure = nil
        let queued = await model.createEpisode(
            title: title.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty,
            maxDurationMinutes: minutes,
            newsItemIds: items.map(\.id),
            keepInBacklog: keepInBacklog
        )
        isSending = false
        if queued {
            onQueued()
            dismiss()
        } else {
            failure = model.connectionMessage ?? "The episode could not be made. Try again."
        }
    }
}

/// Phase 1's only ingestion route, on the phone: paste a newsletter in.
struct PasteView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var title = ""
    @State private var text = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Where it came from", text: $title)
                        .font(Theme.body(16))
                }
                .listRowBackground(Theme.surface)
                Section {
                    TextEditor(text: $text)
                        .font(Theme.body(16))
                        .scrollContentBackground(.hidden)
                        .frame(minHeight: 220)
                } header: {
                    Text("Text").brandLabel()
                }
                .listRowBackground(Theme.surface)
            }
            .brandGround()
            .foregroundStyle(Theme.ink)
            .navigationTitle("Paste in")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") {
                        Task {
                            await model.paste(title: title, text: text)
                            dismiss()
                        }
                    }
                    .disabled(
                        title.trimmingCharacters(in: .whitespaces).isEmpty
                            || text.trimmingCharacters(in: .whitespaces).isEmpty
                    )
                }
            }
        }
        .preferredColorScheme(.light)
        .tint(Theme.ink)
    }
}
