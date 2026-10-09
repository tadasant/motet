#if DEBUG
import Foundation
import MotetKit
import SwiftUI

/// **The playback probe, drawn for a device-farm screen recording** (motet#152).
///
/// A farm recording has no audio track, so on a real phone in a farm the only channel out is
/// pixels — and the only other thing its driver can read is the accessibility tree. This is
/// both: a few fixed rows in large white-on-black monospace, updating as often as
/// `PlaybackProbeReporter` samples (twice a second while playing), and one accessibility
/// element per row whose label and value are the same short token.
///
/// **`#if DEBUG`, which is what keeps it out of every build a listener can install.** The
/// TestFlight / App Store archive is the `Release` configuration (`ios/bin/testflight`),
/// which does not define `DEBUG`, so this type does not exist in that binary — there is no
/// flag to get wrong at runtime. `ios/bin/testflight check` asserts it by looking for the
/// launch argument's bytes in the archived executable. `Debug` and `Staging` define
/// `DEBUG`, and in those it still renders only when launched with
/// `-MotetAudioReadout`, so a developer build looks like the app.
///
/// **It reads, and never touches playback.** Its whole input is two values `AppModel`
/// already publishes — the probe and the Live snapshot — so it cannot block, throw into or
/// slow `play`: nothing here is awaited by anything on the playback path.
enum AudioReadoutGate {
    /// `-MotetAudioReadout` on the launch arguments.
    static let launchArgument = "-MotetAudioReadout"

    static var isRequested: Bool {
        ProcessInfo.processInfo.arguments.contains(launchArgument)
    }
}

struct AudioReadoutView: View {
    let readout: AudioReadout

    init(probe: PlaybackProbe, live: LiveSnapshot?) {
        readout = AudioReadout(probe: probe, live: live)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            row("MODE", readout.mode.rawValue, id: AudioReadout.Identifier.mode)
            row("VERDICT", readout.verdict.rawValue, id: AudioReadout.Identifier.verdict, emphasis: true)
            row("WHY", readout.reason, id: AudioReadout.Identifier.reason)
            row("TRANSPORT", readout.transport.rawValue, id: AudioReadout.Identifier.transport)
            row(
                "POSITION", readout.positionText, id: AudioReadout.Identifier.position,
                value: String(readout.positionMs)
            )
            row("CLOCK", readout.clock.rawValue, id: AudioReadout.Identifier.clock)
            row("RMS", "\(readout.rmsText) \(bar)", id: AudioReadout.Identifier.rms, value: readout.rmsText)
            row("ROUTE", readout.route, id: AudioReadout.Identifier.route)
            // The whole line, for a driver that would rather parse one element than eight.
            // Small, because a recording does not need it and a driver does not read pixels.
            Text(readout.summary)
                .font(.system(size: 9, design: .monospaced))
                .foregroundStyle(.white.opacity(0.75))
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(readout.summary)
                .accessibilityValue(readout.summary)
                .accessibilityIdentifier(AudioReadout.Identifier.summary)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        // Pure black and white, because a farm video is compressed and colour is the first
        // thing compression smears. Nothing here is a brand surface.
        .background(Color.black, in: RoundedRectangle(cornerRadius: 10))
        .environment(\.colorScheme, .dark)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(AudioReadout.Identifier.container)
        // A readout, never a control: it must not take a tap meant for the player.
        .allowsHitTesting(false)
    }

    /// Ten cells, as text rather than as a drawn shape, so the level survives OCR as well as
    /// a glance. `--` where nothing measured, so an absent tap does not look like silence.
    private var bar: String {
        guard let fraction = readout.barFraction else { return "[----------]" }
        let filled = Int((fraction * 10).rounded())
        return "[" + String(repeating: "#", count: filled) + String(repeating: ".", count: 10 - filled) + "]"
    }

    private func row(_ name: String, _ text: String, id: String, value: String? = nil, emphasis: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(name)
                .font(.system(size: 13, weight: .semibold, design: .monospaced))
                .foregroundStyle(.white.opacity(0.7))
                .frame(width: 92, alignment: .leading)
                .accessibilityHidden(true)
            Text(emphasis ? text.uppercased() : text)
                .font(.system(size: emphasis ? 28 : 20, weight: .bold, design: .monospaced))
                .foregroundStyle(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.5)
                // Label and value are both the machine token, so a driver can assert on either
                // without knowing how it is drawn.
                .accessibilityLabel(value ?? text)
                .accessibilityValue(value ?? text)
                .accessibilityIdentifier(id)
        }
    }
}
#endif
