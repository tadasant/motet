import MotetKit
import SwiftUI

/// The visual half of read state.
///
/// Invariant 5: this list and the audio write the same fact. Marking something read here is
/// the same column that listening past it sets, and the row can be put *back* — the backlog
/// is the product's memory, and being unable to undo is worse than never having marked it.
struct BacklogView: View {
    @EnvironmentObject private var model: AppModel
    @State private var showingRead = false
    @State private var isPasting = false

    private var visibleItems: [NewsItemResponse] {
        showingRead ? model.newsItems : model.newsItems.filter { !$0.read }
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ConnectionBanner(message: model.connectionMessage)
                List {
                    if visibleItems.isEmpty {
                        ContentUnavailableView {
                            VStack(spacing: 12) {
                                VoiceDots(size: 8)
                                Text(showingRead ? "Nothing here" : "All caught up")
                                    .font(Theme.display(26, relativeTo: .title2))
                                    .foregroundStyle(Theme.ink)
                            }
                        } description: {
                            Text("Paste in a newsletter, bookmark or thread to start a backlog.")
                                .font(Theme.body(15, relativeTo: .subheadline))
                                .foregroundStyle(Theme.inkSoft)
                        }
                        .listRowBackground(Theme.parchment)
                        .listRowSeparator(.hidden)
                    }
                    ForEach(visibleItems, id: \.id) { item in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.title)
                                .font(Theme.display(18, relativeTo: .headline))
                                .foregroundStyle(item.read ? Theme.inkSoft : Theme.ink)
                            Text(item.summary)
                                .font(Theme.body(15, relativeTo: .subheadline))
                                .foregroundStyle(item.read ? Theme.inkMute : Theme.inkSoft)
                        }
                        .padding(.vertical, 6)
                        .listRowBackground(Theme.parchment)
                        .listRowSeparatorTint(Theme.rule)
                        .swipeActions(edge: .leading) {
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
                .listStyle(.plain)
                .brandGround()
                .refreshable { await model.refresh() }
            }
            .background(Theme.parchment)
            .navigationTitle("Backlog")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { isPasting = true } label: {
                        Label("Paste in", systemImage: "doc.on.clipboard")
                    }
                }
                ToolbarItem(placement: .topBarLeading) {
                    Toggle("Show read", isOn: $showingRead)
                        .toggleStyle(PillToggleStyle())
                }
            }
            .sheet(isPresented: $isPasting) { PasteView() }
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
