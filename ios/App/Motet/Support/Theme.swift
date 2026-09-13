import SwiftUI
import UIKit

/// The Polyphony brand, as SwiftUI values. `brand/GUIDELINES.md` is the authority and
/// `brand/polyphony/index.html` holds the exact numbers; this file only restates them.
///
/// **The four voice hues mean "which source is singing", never a status.** Pending, done,
/// disabled and empty are ink at an opacity. Vermilion is the one hue that may double as
/// error red, because it is the natural red.
///
/// **Dark mode is not designed**, so the app is held in light mode at the root
/// (`.preferredColorScheme(.light)`) rather than inventing a palette the guidelines do not
/// have.
enum Theme {
    // MARK: - Ground and ink

    static let parchment = Color(red: 0xF4 / 255, green: 0xEF / 255, blue: 0xE6 / 255)
    static let surface = Color(red: 0xEA / 255, green: 0xE3 / 255, blue: 0xD6 / 255)
    static let ink = Color(red: 0x1B / 255, green: 0x1A / 255, blue: 0x2E / 255)
    static let inkSoft = ink.opacity(0.66)
    static let inkMute = ink.opacity(0.42)
    static let rule = ink.opacity(0.14)

    // MARK: - The four voices, always in this order

    static let vermilion = Color(red: 0xD6 / 255, green: 0x4B / 255, blue: 0x2A / 255)
    static let ochre = Color(red: 0xD9 / 255, green: 0xA4 / 255, blue: 0x41 / 255)
    static let teal = Color(red: 0x2A / 255, green: 0x7F / 255, blue: 0x86 / 255)
    static let plum = Color(red: 0x6B / 255, green: 0x3E / 255, blue: 0x86 / 255)

    static let voices: [Color] = [vermilion, ochre, teal, plum]

    /// The played portion of a scrubber.
    static var voiceGradient: LinearGradient {
        LinearGradient(colors: voices, startPoint: .leading, endPoint: .trailing)
    }

    /// The only status colour that is a hue.
    static let error = vermilion

    // MARK: - Radii

    static let smallRadius: CGFloat = 12
    static let largeRadius: CGFloat = 20

    // MARK: - UIKit equivalents, for the bars SwiftUI cannot restyle

    static var uiParchment: UIColor { UIColor(red: 0xF4 / 255, green: 0xEF / 255, blue: 0xE6 / 255, alpha: 1) }
    static var uiInk: UIColor { UIColor(red: 0x1B / 255, green: 0x1A / 255, blue: 0x2E / 255, alpha: 1) }
    static var uiInkMute: UIColor { UIColor(red: 0x1B / 255, green: 0x1A / 255, blue: 0x2E / 255, alpha: 0.42) }
    static var uiRule: UIColor { UIColor(red: 0x1B / 255, green: 0x1A / 255, blue: 0x2E / 255, alpha: 0.14) }

    /// Navigation and tab bars on parchment, titles in Fraunces, labels in Instrument Sans.
    ///
    /// `UIAppearance` rather than SwiftUI modifiers because SwiftUI has no way to set a
    /// navigation title's font. Called once, before the first scene renders.
    @MainActor
    static func applyBarAppearance() {
        let navigation = UINavigationBarAppearance()
        navigation.configureWithOpaqueBackground()
        navigation.backgroundColor = uiParchment
        navigation.shadowColor = uiRule
        navigation.largeTitleTextAttributes = [
            .font: BrandFont.displayUIFont(size: 34, opticalSize: 36),
            .foregroundColor: uiInk,
        ]
        navigation.titleTextAttributes = [
            .font: BrandFont.displayUIFont(size: 19, opticalSize: 18),
            .foregroundColor: uiInk,
        ]
        let buttons = UIBarButtonItemAppearance()
        buttons.normal.titleTextAttributes = [
            .font: BrandFont.sansUIFont(size: 17, weight: 500),
            .foregroundColor: uiInk,
        ]
        navigation.buttonAppearance = buttons
        navigation.doneButtonAppearance = buttons

        let bar = UINavigationBar.appearance()
        bar.standardAppearance = navigation
        bar.scrollEdgeAppearance = navigation
        bar.compactAppearance = navigation
        bar.tintColor = uiInk

        let tabs = UITabBarAppearance()
        tabs.configureWithOpaqueBackground()
        tabs.backgroundColor = uiParchment
        tabs.shadowColor = uiRule
        let item = UITabBarItemAppearance()
        let labelFont = BrandFont.sansUIFont(size: 11, weight: 600)
        item.normal.iconColor = uiInkMute
        item.normal.titleTextAttributes = [.font: labelFont, .foregroundColor: uiInkMute]
        item.selected.iconColor = uiInk
        item.selected.titleTextAttributes = [.font: labelFont, .foregroundColor: uiInk]
        tabs.stackedLayoutAppearance = item
        tabs.inlineLayoutAppearance = item
        tabs.compactInlineLayoutAppearance = item

        let tabBar = UITabBar.appearance()
        tabBar.standardAppearance = tabs
        tabBar.scrollEdgeAppearance = tabs
        tabBar.tintColor = uiInk
    }
}

// MARK: - Type roles

extension Theme {
    /// Screen and section headings: Fraunces, `SOFT 100`, `opsz` matched to size.
    static func display(_ size: CGFloat, relativeTo style: Font.TextStyle = .title2) -> Font {
        BrandFont.display(size: size, relativeTo: style)
    }

    /// Prose captions and asides: Fraunces italic.
    static func aside(_ size: CGFloat, relativeTo style: Font.TextStyle = .footnote) -> Font {
        BrandFont.display(size: size, italic: true, relativeTo: style)
    }

    /// Body and UI text: Instrument Sans.
    static func body(
        _ size: CGFloat = 16, weight: CGFloat = 400, relativeTo style: Font.TextStyle = .body
    ) -> Font {
        BrandFont.sans(size: size, weight: weight, relativeTo: style)
    }

    /// Small-caps labels: Instrument Sans 600, used with `.brandLabel()`.
    static func label(_ size: CGFloat = 12) -> Font {
        BrandFont.sans(size: size, weight: 600, relativeTo: .caption)
    }
}

// MARK: - Components

extension View {
    /// An uppercase Instrument Sans label with wide tracking, in ink-mute.
    func brandLabel(size: CGFloat = 12, color: Color = Theme.inkMute) -> some View {
        font(Theme.label(size))
            .textCase(.uppercase)
            .tracking(size * 0.14)
            .foregroundStyle(color)
    }

    /// A card: a fill, a 1pt rule border, no heavy shadow.
    func brandCard(
        fill: Color = Theme.parchment, radius: CGFloat = Theme.smallRadius
    ) -> some View {
        background(
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(fill)
                .overlay(
                    RoundedRectangle(cornerRadius: radius, style: .continuous)
                        .strokeBorder(Theme.rule, lineWidth: 1)
                )
        )
    }

    /// A list or form on the parchment ground rather than the system grouped background.
    func brandGround() -> some View {
        scrollContentBackground(.hidden)
            .background(Theme.parchment)
    }
}

/// The four voice dots, as the eyebrow draws them.
struct VoiceDots: View {
    var size: CGFloat = 6

    var body: some View {
        HStack(spacing: size * 0.66) {
            ForEach(0..<Theme.voices.count, id: \.self) { index in
                Circle().fill(Theme.voices[index]).frame(width: size, height: size)
            }
        }
        .accessibilityHidden(true)
    }
}

/// The wordmark: `motet`, lowercase, Fraunces italic. Never uppercase, never the sans.
struct Wordmark: View {
    var size: CGFloat = 24

    var body: some View {
        Text(verbatim: "motet")
            .font(BrandFont.wordmark(size: size))
            .tracking(size * -0.03)
            .foregroundStyle(Theme.ink)
            .accessibilityLabel("Motet")
    }
}

/// The primary button: an ink capsule, parchment text, a four-hue hairline along the bottom.
struct PrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.body(16, weight: 600))
            .foregroundStyle(Theme.parchment)
            .padding(.vertical, 14)
            .padding(.horizontal, 24)
            .frame(maxWidth: .infinity)
            .background(Theme.ink.opacity(isEnabled ? 1 : 0.42))
            .overlay(alignment: .bottom) {
                if isEnabled {
                    HStack(spacing: 0) {
                        ForEach(0..<Theme.voices.count, id: \.self) { index in
                            Theme.voices[index]
                        }
                    }
                    .frame(height: 3)
                    .opacity(0.95)
                }
            }
            .clipShape(Capsule())
            .opacity(configuration.isPressed ? 0.85 : 1)
    }
}

/// A pill: fully rounded, a rule border, or a surface fill when `filled`.
struct Pill<Content: View>: View {
    var filled = false
    @ViewBuilder var content: Content

    var body: some View {
        HStack(spacing: 7) { content }
            .font(Theme.body(13, weight: 500, relativeTo: .footnote))
            .monospacedDigit()
            .foregroundStyle(Theme.ink)
            .padding(.horizontal, 12)
            .frame(minHeight: 32)
            .background(Capsule().fill(filled ? Theme.surface : Color.clear))
            .overlay(Capsule().strokeBorder(filled ? Color.clear : Theme.rule, lineWidth: 1))
            .contentShape(Capsule())
    }
}

/// A toggle drawn as a pill: filled when on.
struct PillToggleStyle: ToggleStyle {
    func makeBody(configuration: Configuration) -> some View {
        Button {
            configuration.isOn.toggle()
        } label: {
            Pill(filled: configuration.isOn) { configuration.label }
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(configuration.isOn ? .isSelected : [])
    }
}
