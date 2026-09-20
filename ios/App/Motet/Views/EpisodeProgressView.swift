import MotetKit
import SwiftUI

/// Where an episode is between "make it" and a file to play: the step, the count once
/// there is one, how long it has taken against how long one usually takes, and a bar.
/// `EpisodeProgress.describe` decides every word — this decides only the layout.
///
/// Deliberately a second view rather than a reuse of `SyncProgressView`: that one belongs
/// to the Sources screen and takes `SourceStatus.SyncDescription`, which has no `timing`
/// line. The SPA shares one `StageProgress` component between the two because it can
/// share a type; here the two readings are two types, and the thirty lines below are
/// cheaper than a protocol over them.
struct EpisodeProgressView: View {
    let description: EpisodeProgress.Description

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(description.headline)
                .font(Theme.body(15, weight: 600, relativeTo: .subheadline))
                .foregroundStyle(description.tone == .error ? Theme.errorText : Theme.ink)
            if description.tone != .done {
                // An indeterminate *linear* view on iOS is a still, empty track, so before
                // there is a count the spinner is what says "working" without a number.
                Group {
                    if let fraction = description.fraction {
                        ProgressView(value: fraction)
                    } else if description.tone == .working {
                        ProgressView().controlSize(.small)
                    }
                }
                .tint(tint)
                .accessibilityLabel(description.count ?? description.headline)
            }
            if let count = description.count {
                Text(count)
                    .font(Theme.body(14, relativeTo: .footnote))
                    .monospacedDigit()
            }
            if let timing = description.timing {
                Text(timing)
                    .font(Theme.body(13, relativeTo: .caption))
                    .monospacedDigit()
                    .foregroundStyle(Theme.inkSoft)
            }
            if let detail = description.detail {
                Text(detail)
                    .font(Theme.body(13, relativeTo: .caption))
                    .foregroundStyle(description.tone == .error ? Theme.errorText : Theme.inkSoft)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }

    private var tint: Color {
        switch description.tone {
        case .working, .done: return Theme.teal
        case .stalled: return Theme.inkSoft
        case .error: return Theme.vermilion
        }
    }
}
