import Foundation
import MotetKit

/// Small formatters the screens share.
enum Format {
    /// `mm:ss`, or `h:mm:ss` past an hour. Podcast timestamps, not durations in prose.
    static func time(_ milliseconds: Int) -> String {
        let totalSeconds = max(0, milliseconds) / 1_000
        let hours = totalSeconds / 3_600
        let minutes = (totalSeconds % 3_600) / 60
        let seconds = totalSeconds % 60
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, seconds)
        }
        return String(format: "%d:%02d", minutes, seconds)
    }

    /// "18 min" — what a list row needs to answer "do I have time for this?".
    static func duration(_ milliseconds: Int) -> String {
        let minutes = Int((Double(max(0, milliseconds)) / 60_000).rounded())
        return minutes < 1 ? "under a minute" : "\(minutes) min"
    }

    /// "Sep 12, 5:04 PM" — when a newsletter arrived, on a provenance line.
    static func arrived(_ date: Date) -> String {
        date.formatted(date: .abbreviated, time: .shortened)
    }

    static func bytes(_ count: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(count), countStyle: .file)
    }

    static func rate(_ rate: Double) -> String {
        rate == rate.rounded() ? String(format: "%.0f×", rate) : String(format: "%.2g×", rate)
    }
}

extension String {
    /// Nil for a string with nothing in it — what an omitted field is on the wire.
    ///
    /// A blank episode title is not "call it the empty string": it is "you name it", and
    /// the server is the one place that name is composed.
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
