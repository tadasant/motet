import MotetKit
import SwiftUI

/// A story's provenance: what it says, and which write-ups it was made from.
///
/// A backlog row names a story with one line, and for a merged one that line is dedup's —
/// written by a model, about several newsletters at once. This screen is the answer to
/// "says who": the title and summary at the top, then every contributing source item with
/// its own untouched title, when it arrived, and the opening of its text.
///
/// The SPA's `NewsItemDetail`, rule for rule, off the same route: one request rather than
/// one per source, and a preview rather than whole bodies.
struct NewsItemDetailView: View {
    @EnvironmentObject private var model: AppModel
    let item: NewsItemResponse

    @State private var detail: NewsItemDetailResponse?
    @State private var failure: String?

    var body: some View {
        List {
            Section {
                Text(detail?.displayTitle ?? item.listTitle)
                    .font(Theme.display(24, relativeTo: .title2))
                    .foregroundStyle(Theme.ink)
                Text(detail?.summary ?? item.summary)
                    .font(Theme.body(15, relativeTo: .subheadline))
                    .foregroundStyle(Theme.inkSoft)
            }
            .listRowBackground(Theme.surface)

            Section {
                if let detail {
                    ForEach(detail.sources, id: \.id) { source in
                        sourceRow(source, merged: detail.sources.count > 1)
                    }
                } else if failure != nil {
                    // The route is not there, or could not be reached. The row this screen
                    // was opened from already carries every source's title, so the titles
                    // are shown and the previews are the thing that is missing — said once,
                    // rather than an error page over data the app is holding.
                    ForEach(item.sources, id: \.id) { source in
                        Text(source.title.isEmpty ? "(untitled)" : source.title)
                            .font(Theme.display(17, relativeTo: .headline))
                            .foregroundStyle(Theme.ink)
                    }
                    Text(failure ?? "Previews couldn\u{2019}t be loaded.")
                        .font(Theme.body(13, relativeTo: .footnote))
                        .foregroundStyle(Theme.inkSoft)
                } else {
                    Text("Loading…")
                        .font(Theme.body(14, relativeTo: .footnote))
                        .foregroundStyle(Theme.inkSoft)
                }
            } header: {
                Text(sourcesHeader).brandLabel()
            }
            .listRowBackground(Theme.surface)
        }
        .listStyle(.plain)
        .brandGround()
        .navigationTitle("Story")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    private var sourcesHeader: String {
        let count = detail?.sources.count ?? item.sourceCount
        return count == 1 ? "Source" : "\(count) sources"
    }

    @ViewBuilder
    private func sourceRow(_ source: NewsItemSourceDetailResponse, merged: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(source.title.isEmpty ? "(untitled)" : source.title)
                .font(Theme.display(17, relativeTo: .headline))
                .foregroundStyle(Theme.ink)
            Text(
                [
                    source.sourceName,
                    Format.arrived(source.receivedAt),
                    merged ? (source.position == 0 ? "started this story" : "merged in") : nil,
                ]
                .compactMap { $0 }
                .joined(separator: " · ")
            )
            .brandLabel(size: 11)
            Text(source.preview)
                .font(Theme.aside(15))
                .foregroundStyle(Theme.inkSoft)
        }
        .padding(.vertical, 6)
    }

    private func load() async {
        do {
            detail = try await model.newsItem(id: item.id)
            failure = nil
        } catch let error as MotetError {
            failure = error.description
        } catch {
            failure = String(describing: error)
        }
    }
}
