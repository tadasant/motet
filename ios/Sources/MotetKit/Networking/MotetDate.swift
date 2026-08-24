import Foundation

/// Date coding for the contract's `format: date-time` fields.
///
/// `.iso8601` alone is not enough. FastAPI serialises `datetime` with **microseconds**, and
/// `ISO8601DateFormatter` accepts at most fractional seconds it recognises; a naive
/// datetime (no offset) also reaches the wire when a row was written without one. Both
/// shapes are real, and a decoder that rejects either turns "the episode list" into an
/// error screen. So the parser is deliberately tolerant, and the *encoder* is not — we
/// always write the fully-specified form.
public enum MotetDate {
    /// `ISO8601DateFormatter` is a reference type Foundation does not declare `Sendable`,
    /// and building one per timestamp shows up when decoding a list of episodes. One
    /// instance behind a lock is the cheap correct answer.
    private static let lock = NSLock()

    nonisolated(unsafe) private static let withFraction: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    nonisolated(unsafe) private static let withoutFraction: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    /// Parse an ISO 8601 timestamp, tolerating sub-second precision beyond milliseconds and
    /// a missing UTC offset (which is read as UTC — the API stores and serves UTC).
    public static func parse(_ text: String) -> Date? {
        lock.withLock { parseLocked(text) }
    }

    private static func parseLocked(_ text: String) -> Date? {
        if let date = withFraction.date(from: text) ?? withoutFraction.date(from: text) {
            return date
        }

        // Trim sub-millisecond digits: "…:00.123456+00:00" -> "…:00.123+00:00".
        var trimmed = text
        if let dot = text.firstIndex(of: ".") {
            let afterDot = text.index(after: dot)
            let digits = text[afterDot...].prefix { $0.isNumber }
            if digits.count > 3 {
                let cut = text.index(afterDot, offsetBy: 3)
                trimmed = String(text[..<cut]) + String(text[text.index(afterDot, offsetBy: digits.count)...])
            }
        }
        if let date = withFraction.date(from: trimmed) ?? withoutFraction.date(from: trimmed) {
            return date
        }

        // No offset at all: treat as UTC rather than as local time, which would shift every
        // timestamp by the walker's timezone.
        let assumedUTC = trimmed + "Z"
        return withFraction.date(from: assumedUTC) ?? withoutFraction.date(from: assumedUTC)
    }

    public static func format(_ date: Date) -> String {
        lock.withLock { withFraction.string(from: date) }
    }

    public static var decodingStrategy: JSONDecoder.DateDecodingStrategy {
        .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            guard let date = parse(text) else {
                throw DecodingError.dataCorruptedError(
                    in: container, debugDescription: "not an ISO 8601 date: \(text)"
                )
            }
            return date
        }
    }

    public static var encodingStrategy: JSONEncoder.DateEncodingStrategy {
        .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(format(date))
        }
    }

    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = decodingStrategy
        return decoder
    }

    public static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = encodingStrategy
        return encoder
    }
}
