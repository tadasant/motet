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

    static func bytes(_ count: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(count), countStyle: .file)
    }

    static func rate(_ rate: Double) -> String {
        rate == rate.rounded() ? String(format: "%.0f×", rate) : String(format: "%.2g×", rate)
    }
}
