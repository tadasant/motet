import MotetKit
import SwiftUI

/// "Pulled in, waiting for you" — the gate between free work and paid work, on the phone.
///
/// **This screen is what motet#139 was really about.** Connecting a mailbox does the free,
/// deterministic work at once (poll, fetch, extract) and stops; inference is spent only when
/// a person picks items and says ingest (motet#91). The web SPA has had that panel since
/// then and the app never did — it had the *count*, on the Sources screen, and nothing
/// behind it. So 55 newsletters that had been pulled in correctly were, from an iPhone,
/// indistinguishable from 55 that were never synced: the Backlog tab lists news items, and a
/// held item is deliberately not one yet.
///
/// It is the SPA's `Held` panel, rule for rule: oldest message first, select what you want,
/// one button that spends and one that discards, and a dismiss that asks first because
/// nothing un-dismisses.
struct HeldItemsView: View {
    @EnvironmentObject private var model: SourcesModel
    /// Narrow the list to one mailbox when this was opened from that mailbox; `nil` shows
    /// every source's, which is what the Backlog tab links to.
    var sourceId: String? = nil

    @State private var selection: Set<String> = []
    @State private var working = false
    @State private var notice: String?
    @State private var actionError: String?
    @State private var confirmingDismiss = false

    private var items: [HeldSourceItemResponse] {
        guard let sourceId else { return model.held }
        return model.held.filter { $0.sourceId == sourceId }
    }

    private var selected: [HeldSourceItemResponse] { items.filter { selection.contains($0.id) } }
    private var selectedChars: Int { selected.reduce(0) { $0 + $1.chars } }

    var body: some View {
        List {
            Section {
                Text("Nothing here has cost inference yet. Ingest what you want; dismiss what you do not.")
                    .font(Theme.body(14, relativeTo: .footnote))
                    .foregroundStyle(Theme.inkSoft)
                if let notice {
                    Text(notice).font(Theme.body(14, relativeTo: .footnote))
                }
                if let actionError {
                    Text(actionError)
                        .font(Theme.body(14, relativeTo: .footnote))
                        .foregroundStyle(Theme.errorText)
                }
            }
            .listRowBackground(Theme.parchment)

            if items.isEmpty {
                Section {
                    Text("Nothing is waiting. What a sync pulls in lands here first.")
                        .font(Theme.aside(16))
                        .foregroundStyle(Theme.inkSoft)
                }
                .listRowBackground(Theme.surface)
            } else {
                Section {
                    ForEach(items, id: \.id) { item in
                        Button {
                            toggle(item.id)
                        } label: {
                            row(item)
                        }
                        .buttonStyle(.plain)
                    }
                } header: {
                    HStack {
                        Text(countLabel).brandLabel()
                        Spacer()
                        Button(allSelected ? "Select none" : "Select all") { toggleAll() }
                            .font(Theme.body(14, relativeTo: .footnote))
                            .disabled(working)
                    }
                }
                .listRowBackground(Theme.surface)

                Section {
                    Button(ingestLabel) { Task { await ingest() } }
                        .font(Theme.body(16, weight: 600))
                        .disabled(working || selection.isEmpty)
                    Button("Dismiss \(selection.count)", role: .destructive) {
                        confirmingDismiss = true
                    }
                    .disabled(working || selection.isEmpty)
                    if !selection.isEmpty {
                        Text("~\(chars(selectedChars)) → dedup at low effort, each.")
                            .font(Theme.body(14, relativeTo: .footnote))
                            .foregroundStyle(Theme.inkSoft)
                    }
                }
                .listRowBackground(Theme.parchment)
            }
        }
        .brandGround()
        .foregroundStyle(Theme.ink)
        .font(Theme.body(16))
        .navigationTitle("Waiting for you")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await model.refresh() }
        .confirmationDialog(
            "Dismiss \(selection.count) item\(selection.count == 1 ? "" : "s")?",
            isPresented: $confirmingDismiss,
            titleVisibility: .visible
        ) {
            Button("Dismiss", role: .destructive) { Task { await dismissSelected() } }
            Button("Keep", role: .cancel) {}
        } message: {
            Text("They will not be in an episode, and nothing brings them back.")
        }
    }

    private var allSelected: Bool { !items.isEmpty && selection.count == items.count }

    private var countLabel: String {
        "\(items.count) waiting\(selection.isEmpty ? "" : " · \(selection.count) picked")"
    }

    private var ingestLabel: String {
        if working { return "Working…" }
        return selection.isEmpty ? "Ingest now" : "Ingest \(selection.count) now"
    }

    private func row(_ item: HeldSourceItemResponse) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: selection.contains(item.id) ? "checkmark.circle.fill" : "circle")
                .foregroundStyle(selection.contains(item.id) ? Theme.ink : Theme.inkSoft)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.title.isEmpty ? "(untitled)" : item.title)
                    .font(Theme.body(16, weight: 600))
                Text(item.preview)
                    .font(Theme.body(14, relativeTo: .footnote))
                    .foregroundStyle(Theme.inkSoft)
                    .lineLimit(2)
                Text("\(item.sourceName) · \(item.receivedAt.formatted(date: .abbreviated, time: .shortened)) · \(chars(item.chars))")
                    .font(Theme.aside(13))
                    .foregroundStyle(Theme.inkSoft)
            }
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(traits(for: item))
    }

    private func traits(for item: HeldSourceItemResponse) -> AccessibilityTraits {
        var traits: AccessibilityTraits = .isButton
        if selection.contains(item.id) { traits.insert(.isSelected) }
        return traits
    }

    private func chars(_ count: Int) -> String {
        String(format: "%.1fk chars", Double(count) / 1000)
    }

    private func toggle(_ id: String) {
        if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
    }

    private func toggleAll() {
        selection = allSelected ? [] : Set(items.map(\.id))
    }

    private func ingest() async {
        await act {
            let result = try await model.ingest(ids: Array(selection))
            return "\(result.queued) queued for processing"
                + (result.skipped > 0 ? " (\(result.skipped) skipped)." : ".")
        }
    }

    private func dismissSelected() async {
        await act {
            let result = try await model.dismiss(ids: Array(selection))
            return "\(result.dismissed) dismissed"
                + (result.skipped > 0 ? " (\(result.skipped) skipped)." : ".")
        }
    }

    /// Both actions have the same shape: spend the selection, say what happened, and clear
    /// it — the model's own refresh is what takes the acted-on rows off this list.
    private func act(_ run: () async throws -> String) async {
        working = true
        actionError = nil
        notice = nil
        do {
            notice = try await run()
            selection = []
        } catch {
            actionError = SourcesModel.describe(error)
        }
        working = false
    }
}
