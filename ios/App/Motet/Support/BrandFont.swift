import CoreGraphics
import CoreText
import SwiftUI
import UIKit
import os

/// The brand's two typefaces, Fraunces and Instrument Sans, self-hosted under the OFL.
///
/// **Registered at runtime rather than through `UIAppFonts`**, so that bundling them needs no
/// Info.plist change. `register()` is called once at launch; the registration is a lazy
/// `static let`, so any later caller is a no-op and a concurrent first call is safe.
///
/// **Both files are variable fonts, and their defaults are not what the brand wants.**
/// Fraunces' default instance is *9pt Black*, so every Fraunces font sets `wght`, `opsz`
/// matched to its size, and `SOFT 100` explicitly, through a font-variation descriptor.
///
/// **Text always renders.** A face that failed to register falls back to the system serif
/// (Fraunces) or the system sans (Instrument Sans) at the same size and weight, and says so
/// in the log once, at registration — not per view.
enum BrandFont {
    enum Face: String, CaseIterable, Sendable {
        case fraunces = "Fraunces"
        case frauncesItalic = "Fraunces-Italic"
        case instrumentSans = "InstrumentSans"
    }

    private static let logger = Logger(subsystem: "com.getmotet.app", category: "fonts")

    /// The PostScript name of each face that registered and resolves.
    private static let registered: [Face: String] = registerBundledFonts()

    /// Register the bundled fonts. Idempotent.
    static func register() {
        _ = registered
    }

    // MARK: - SwiftUI

    /// Fraunces, `SOFT 100`, weight 400 unless told otherwise, `opsz` matched to size.
    static func display(
        size: CGFloat,
        italic: Bool = false,
        weight: CGFloat = 400,
        opticalSize: CGFloat? = nil,
        relativeTo style: Font.TextStyle = .body
    ) -> Font {
        font(
            displayUIFont(size: size, opticalSize: opticalSize, italic: italic, weight: weight),
            relativeTo: style
        )
    }

    /// The wordmark's cut: Fraunces italic at `opsz 72`, as the reference page sets it.
    static func wordmark(size: CGFloat) -> Font {
        font(
            displayUIFont(size: size, opticalSize: 72, italic: true, weight: 500),
            relativeTo: .title2
        )
    }

    /// Instrument Sans at a weight from 400 to 700.
    static func sans(
        size: CGFloat, weight: CGFloat = 400, relativeTo style: Font.TextStyle = .body
    ) -> Font {
        font(sansUIFont(size: size, weight: weight), relativeTo: style)
    }

    // MARK: - UIKit

    static func displayUIFont(
        size: CGFloat, opticalSize: CGFloat? = nil, italic: Bool = false, weight: CGFloat = 400
    ) -> UIFont {
        let face: Face = italic ? .frauncesItalic : .fraunces
        let opsz = min(max(opticalSize ?? size, 9), 144)
        let axes: [Int: CGFloat] = [
            VariationAxis.wght: weight,
            VariationAxis.opsz: opsz,
            VariationAxis.soft: 100,
            // The file's default is WONK 1, the leaning "wonky" alternates; the web's
            // Google-served subset renders without them, so set it rather than differ.
            VariationAxis.wonk: 0,
        ]
        if let font = variableFont(face: face, size: size, axes: axes) {
            return font
        }
        return systemFont(size: size, weight: weight, design: .serif, italic: italic)
    }

    static func sansUIFont(size: CGFloat, weight: CGFloat = 400) -> UIFont {
        if let font = variableFont(
            face: .instrumentSans, size: size, axes: [VariationAxis.wght: min(max(weight, 400), 700)]
        ) {
            return font
        }
        return systemFont(size: size, weight: weight, design: .default, italic: false)
    }

    // MARK: - Internals

    /// Variation axis identifiers, as the FourCC integers Core Text expects.
    private enum VariationAxis {
        static let wght = 0x7767_6874  // 'wght'
        static let opsz = 0x6F70_737A  // 'opsz'
        static let soft = 0x534F_4654  // 'SOFT'
        static let wonk = 0x574F_4E4B  // 'WONK'
    }

    private static func font(_ uiFont: UIFont, relativeTo style: Font.TextStyle) -> Font {
        let scaled = UIFontMetrics(forTextStyle: uiTextStyle(style)).scaledFont(for: uiFont)
        return Font(scaled as CTFont)
    }

    private static func variableFont(face: Face, size: CGFloat, axes: [Int: CGFloat]) -> UIFont? {
        guard let name = registered[face] else { return nil }
        var variations: [NSNumber: NSNumber] = [:]
        for (axis, value) in axes {
            variations[NSNumber(value: axis)] = NSNumber(value: Double(value))
        }
        let variationKey = UIFontDescriptor.AttributeName(rawValue: kCTFontVariationAttribute as String)
        let descriptor = UIFontDescriptor(name: name, size: size)
            .addingAttributes([variationKey: variations])
        return UIFont(descriptor: descriptor, size: size)
    }

    private static func systemFont(
        size: CGFloat, weight: CGFloat, design: UIFontDescriptor.SystemDesign, italic: Bool
    ) -> UIFont {
        var descriptor = UIFont.systemFont(ofSize: size, weight: uiWeight(weight)).fontDescriptor
        descriptor = descriptor.withDesign(design) ?? descriptor
        if italic {
            descriptor = descriptor.withSymbolicTraits(.traitItalic) ?? descriptor
        }
        return UIFont(descriptor: descriptor, size: size)
    }

    private static func uiWeight(_ weight: CGFloat) -> UIFont.Weight {
        switch weight {
        case ..<450: return .regular
        case ..<550: return .medium
        case ..<650: return .semibold
        default: return .bold
        }
    }

    private static func uiTextStyle(_ style: Font.TextStyle) -> UIFont.TextStyle {
        switch style {
        case .largeTitle: return .largeTitle
        case .title: return .title1
        case .title2: return .title2
        case .title3: return .title3
        case .headline: return .headline
        case .subheadline: return .subheadline
        case .callout: return .callout
        case .footnote: return .footnote
        case .caption: return .caption1
        case .caption2: return .caption2
        default: return .body
        }
    }

    private static func registerBundledFonts() -> [Face: String] {
        var names: [Face: String] = [:]
        for face in Face.allCases {
            guard let url = Bundle.main.url(forResource: face.rawValue, withExtension: "ttf")
                ?? Bundle.main.url(forResource: face.rawValue, withExtension: "ttf", subdirectory: "Fonts")
            else {
                logger.error("font \(face.rawValue, privacy: .public) is not in the bundle; using the system fallback")
                continue
            }
            guard let provider = CGDataProvider(url: url as CFURL),
                  let cgFont = CGFont(provider),
                  let postScriptName = cgFont.postScriptName
            else {
                logger.error("font \(face.rawValue, privacy: .public) could not be read; using the system fallback")
                continue
            }
            let name = postScriptName as String

            var error: Unmanaged<CFError>?
            if !CTFontManagerRegisterFontsForURL(url as CFURL, .process, &error) {
                // "Already registered" is the expected answer on a second call; whether the
                // name resolves below is the actual test.
                let reason = error.map { String(describing: $0.takeRetainedValue()) } ?? "unknown"
                logger.debug("font \(face.rawValue, privacy: .public) registration returned \(reason, privacy: .public)")
            }
            if UIFont(name: name, size: 12) != nil {
                names[face] = name
            } else {
                logger.error("font \(face.rawValue, privacy: .public) did not register; using the system fallback")
            }
        }
        return names
    }
}
