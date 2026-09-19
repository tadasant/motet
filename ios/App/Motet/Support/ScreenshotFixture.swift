#if DEBUG
import Foundation
import MotetKit

/// A Debug-only way to put one screen on a simulator with sample data and no server.
///
/// `-MotetScreenshot <scene>` on the launch arguments renders the backlog in that state
/// instead of the app, with no sign-in and no network. It exists because nothing else can
/// show a screen of this app to someone without a Mac: CI's hosted macOS runner boots a
/// simulator, launches with the argument, and takes the picture. Release builds — which
/// is everything TestFlight ships — do not contain it.
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
        item("n1", "Acme raises $20M Series A",
             "Northwind Ventures led the round, bringing Acme's total funding to $31M.",
             from: "The Download — Tuesday"),
        item("n2", "Regulator opens an inquiry into data retention",
             "The agency confirmed an inquiry into retention practices at three large platforms.",
             from: "Platformer"),
        item("n3", "Chipmaker delays its next fab by a year",
             "Equipment shortages push first production to late 2028, the company said.",
             from: "Stratechery Daily"),
        item("n4", "City council approves the east-side transit line",
             "Construction starts in spring; the line opens in stages from 2029.",
             from: "Morning Brew"),
        item("n5", "A quieter week for open-source AI releases",
             "Fewer model drops, more tooling: evaluation harnesses and inference servers.",
             from: "Import AI"),
    ]

    private static func item(
        _ id: String, _ title: String, _ summary: String, from source: String
    ) -> NewsItemResponse {
        NewsItemResponse(
            createdAt: Date(timeIntervalSince1970: 1_789_800_000),
            id: id,
            read: false,
            sourceItemIds: ["s-\(id)"],
            sources: [NewsItemSourceRef(id: "s-\(id)", title: source)],
            summary: summary,
            title: title
        )
    }
}
#endif
