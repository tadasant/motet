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
                        ContentUnavailableView(
                            showingRead ? "Nothing here" : "All caught up",
                            systemImage: "tray",
                            description: Text("Paste something in to start a backlog.")
                        )
                    }
                    ForEach(visibleItems, id: \.id) { item in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(item.title).font(.headline)
                            Text(item.summary).font(.subheadline).foregroundStyle(.secondary)
                        }
                        .swipeActions(edge: .leading) {
                            Button {
                                Task { await model.setRead(!item.read, newsItem: item) }
                            } label: {
                                Label(
                                    item.read ? "Unread" : "Read",
                                    systemImage: item.read ? "envelope.badge" : "envelope.open"
                                )
                            }
                            .tint(item.read ? .orange : .green)
                        }
                    }
                }
                .listStyle(.plain)
                .refreshable { await model.refresh() }
            }
            .navigationTitle("Backlog")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { isPasting = true } label: {
                        Label("Paste in", systemImage: "doc.on.clipboard")
                    }
                }
                ToolbarItem(placement: .topBarLeading) {
                    Toggle("Show read", isOn: $showingRead)
                        .toggleStyle(.button)
                        .font(.footnote)
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
                TextField("Where it came from", text: $title)
                Section("Text") {
                    TextEditor(text: $text).frame(minHeight: 220)
                }
            }
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
    }
}
