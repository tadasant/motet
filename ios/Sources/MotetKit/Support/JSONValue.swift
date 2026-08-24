import Foundation

/// Any JSON value, for the corners of the contract that are genuinely untyped.
///
/// Exactly two things need it — `ValidationError.ctx` and `ValidationError.input`, FastAPI's
/// 422 detail. The client renders those as text rather than reading them field by field, so
/// modelling them further would be inventing structure the server never promised.
public enum JSONValue: Codable, Hashable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "not a JSON value"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    /// A flat rendering, for putting a validation detail in front of a human.
    public var displayText: String {
        switch self {
        case .null: return "null"
        case .bool(let value): return String(value)
        case .number(let value):
            // `Int(_:)` traps outside `Int`'s range, and this renders a *server error
            // detail* — a crash here would turn a 422 into a crash report.
            guard value == value.rounded(), let whole = Int(exactly: value.rounded()) else {
                return String(value)
            }
            return String(whole)
        case .string(let value): return value
        case .array(let values): return values.map(\.displayText).joined(separator: ", ")
        case .object(let values):
            return values.keys.sorted()
                .map { "\($0): \(values[$0]!.displayText)" }
                .joined(separator: ", ")
        }
    }
}
