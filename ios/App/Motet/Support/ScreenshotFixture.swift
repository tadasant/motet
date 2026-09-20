#if DEBUG
import Foundation
import MotetKit

/// A Debug-only way to put one screen on a simulator with sample data and no server.
///
/// `-MotetScreenshot <scene>` on the launch arguments renders the backlog in that state
/// instead of the app, with no sign-in and no network. It exists because this app has no
/// Mac to be looked at on: a hosted macOS runner can build Debug for a simulator, then
/// `xcrun simctl launch <udid> com.getmotet.app -MotetScreenshot selecting` and
/// `xcrun simctl io <udid> screenshot` take the picture (how motet#130's were taken). Release
/// builds — which is everything TestFlight ships — do not contain it.
enum ScreenshotFixture: String {
    /// The backlog as it opens.
    case backlog
    /// Picking: three stories ticked, the generate bar live.
    case selecting
    /// The sheet the generate bar opens, keep-in-backlog at its default (off).
    case generate
    /// The same sheet with keep-in-backlog switched on.
    case generateKeep = "generate-keep"

    static var current: ScreenshotFixture? {
        let arguments = ProcessInfo.processInfo.arguments
        guard let flag = arguments.firstIndex(of: "-MotetScreenshot"),
              arguments.indices.contains(flag + 1) else { return nil }
        return ScreenshotFixture(rawValue: arguments[flag + 1])
    }

    var isSelecting: Bool { self != .backlog }
    var isGenerating: Bool { self == .generate || self == .generateKeep }
    var keepInBacklog: Bool { self == .generateKeep }
    var selection: Set<String> { isSelecting ? ["n1", "n3", "n4"] : [] }

    static let newsItems: [NewsItemResponse] = [
        // A merged story: dedup's title, and a count saying how many write-ups are behind
        // it. The only row that needs the affordance.
        item("n1", "Acme raises $20M Series A",
             "Northwind Ventures led the round, bringing Acme's total funding to $31M.",
             from: ["The Download — Tuesday", "Import AI"]),
        item("n2", "Regulator opens an inquiry into data retention",
             "The agency confirmed an inquiry into retention practices at three large platforms.",
             from: ["Platformer: the retention inquiry nobody asked for"]),
        item("n3", "Chipmaker delays its next fab by a year",
             "Equipment shortages push first production to late 2028, the company said.",
             from: ["Stratechery Daily — the fab slips again"]),
        item("n4", "City council approves the east-side transit line",
             "Construction starts in spring; the line opens in stages from 2029.",
             from: ["Morning Brew: transit, approved"]),
        item("n5", "A quieter week for open-source AI releases",
             "Fewer model drops, more tooling: evaluation harnesses and inference servers.",
             from: ["Import AI — a quiet week"]),
    ]

    /// The server's own rule, so a fixture row reads exactly as a real one does: one
    /// source is titled by that source, verbatim; several wear dedup's title.
    private static func item(
        _ id: String, _ title: String, _ summary: String, from sources: [String]
    ) -> NewsItemResponse {
        NewsItemResponse(
            createdAt: Date(timeIntervalSince1970: 1_789_800_000),
            displayTitle: sources.count == 1 ? sources[0] : title,
            id: id,
            read: false,
            sourceItemIds: sources.indices.map { "s-\(id)-\($0)" },
            sources: sources.enumerated().map { index, name in
                NewsItemSourceRef(id: "s-\(id)-\(index)", title: name)
            },
            summary: summary,
            title: title
        )
    }
}
#endif
