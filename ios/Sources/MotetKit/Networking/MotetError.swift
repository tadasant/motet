import Foundation

/// Everything that can go wrong between the app and Motet's API.
///
/// `offline` is separated from `transport` on purpose. The whole point of this client is a
/// dog walk with no signal: "the network is down" is an ordinary state the UI shows calmly
/// and the outbox retries later, while `transport` is a genuine surprise worth reporting.
public enum MotetError: Error, Sendable {
    /// No usable connection. Expected, not exceptional.
    case offline
    /// The API is not configured yet — no base URL, or no token.
    case notConfigured
    /// The token was rejected. A 401 or 403.
    case unauthorized
    /// A non-2xx response, with whatever detail the body carried.
    case http(status: Int, detail: String?)
    /// A response that did not match the contract.
    case decoding(String)
    /// The request never completed.
    case transport(Error)

    /// Whether retrying the same request later could plausibly succeed.
    ///
    /// The outbox keys off this: a queued read-state write survives a flat network and is
    /// retried, but a 422 means the request itself is wrong and retrying it forever would
    /// wedge the queue behind an item that can never drain.
    public var isRetryable: Bool {
        switch self {
        case .offline, .transport:
            return true
        case .http(let status, _):
            return status >= 500 || status == 408 || status == 429
        case .notConfigured, .unauthorized, .decoding:
            return false
        }
    }
}

extension MotetError: CustomStringConvertible {
    public var description: String {
        switch self {
        case .offline: return "No connection."
        case .notConfigured: return "Motet is not configured. Add a server URL and token."
        case .unauthorized: return "The API token was rejected."
        case .http(let status, let detail):
            return detail.map { "HTTP \(status): \($0)" } ?? "HTTP \(status)"
        case .decoding(let message): return "Unexpected response: \(message)"
        case .transport(let error): return "Network error: \(error)"
        }
    }
}
