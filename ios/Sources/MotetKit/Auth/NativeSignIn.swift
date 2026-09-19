import Foundation
#if canImport(CryptoKit)
import CryptoKit
#endif

/// Signing the app in through the web sign-in, and collecting the result.
///
/// Chosen by Tadas on 2026-09-13 (AGENTS.md, "The phone signs in through the web sign-in").
/// The app asks the API to start a Google sign-in on its behalf, opens the returned URL in
/// the system sign-in sheet, and waits for a `motet://signed-in?code=…` link. The web app
/// navigates there once Google and the allowlist have both said yes. The app then trades
/// that code, together with the PKCE verifier only it holds, for an ordinary session.
///
/// The session token never travels in a URL. What does is a single-use code that expires in
/// two minutes and is worthless without the verifier, so another app registering the same
/// scheme gains nothing by reading it.
public enum NativeSignIn {
    /// The scheme the sign-in sheet waits for. The API builds the link from the same literal.
    public static let callbackScheme = "motet"
    static let handoffHost = "signed-in"
    /// The path an https handoff lands on, where the deployment serves an
    /// app-site-association file. Keep in step with the API's `NATIVE_HANDOFF_PATH`.
    public static let handoffPath = "/app/signed-in"

    public enum Failure: Error, Equatable, CustomStringConvertible {
        /// The sheet returned something that is not the API's handoff link.
        case notAHandoff
        /// A handoff link with no code in it.
        case missingCode
        /// The redeem answered without a session token.
        case noSession

        public var description: String {
            switch self {
            case .notAHandoff: return "The sign-in came back somewhere unexpected. Try again."
            case .missingCode: return "The sign-in came back without a code. Try again."
            case .noSession: return "The sign-in finished without a session. Try again."
            }
        }
    }

    /// How a sign-in sheet that reported "cancelled" actually ended.
    ///
    /// `ASWebAuthenticationSession` uses one error code, `canceledLogin`, both for a person
    /// dismissing the sheet and for the system refusing to show it — which it does for an
    /// https callback on a domain this device has not verified the app's association with,
    /// among other reasons. Treating both as a person's Cancel is what made "Sign in with
    /// Google" flash "Signing in…" and return to itself with nothing said (2026-09-19): a
    /// refusal is the one outcome that must never be silent.
    public enum SheetEnding: Equatable, Sendable {
        /// Somebody saw the sign-in page and dismissed it. Say nothing.
        case dismissedByPerson
        /// The system ended it before anyone could have used it. Fall back, or say so.
        case refusedBeforeShown
    }

    /// Below this, a "cancelled" sheet was not cancelled by a person: nobody loads Google's
    /// page and presses Cancel inside a second, and a refusal arrives in milliseconds.
    public static let refusalWindow: Duration = .seconds(1)

    /// Which of the two a `canceledLogin` that arrived `elapsed` after opening was.
    public static func classifyCancellation(after elapsed: Duration) -> SheetEnding {
        elapsed < refusalWindow ? .refusedBeforeShown : .dismissedByPerson
    }

    /// The one-time code out of the link the web app navigated to.
    ///
    /// Two shapes are the API's: `motet://signed-in?code=…`, and — where the deployment
    /// serves an app-site-association file naming this app — `https://<host>/app/signed-in?
    /// code=…`, which only an app entitled for that host can be handed.
    ///
    /// `appLinkHost` is the host this sign-in asked for, and the https shape is refused
    /// without it. In practice the system hands back only the host the session named, so
    /// this is defence in depth — but it is a public entry point, and the custom-scheme arm
    /// has always checked its host.
    public static func handoffCode(from url: URL, appLinkHost: String? = nil) throws -> String {
        let isCustomScheme = url.scheme == callbackScheme && url.host == handoffHost
        let isAppLink = url.scheme == "https" && url.path == handoffPath
            && appLinkHost != nil && url.host == appLinkHost
        guard isCustomScheme || isAppLink,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { throw Failure.notAHandoff }
        guard let code = components.queryItems?.first(where: { $0.name == "code" })?.value,
              !code.isEmpty
        else { throw Failure.missingCode }
        return code
    }
}

/// An RFC 7636 verifier and its S256 challenge.
///
/// The hash is CryptoKit's, never one written here. CryptoKit exists only on Apple
/// platforms, so the Linux run of these tests covers the link and the wire, and the macOS
/// job covers the derivation.
public struct PKCEPair: Hashable, Sendable {
    public let verifier: String
    public let challenge: String

    /// base64url without padding, the encoding both halves of RFC 7636 use.
    static func base64URL<Bytes: Sequence>(_ bytes: Bytes) -> String where Bytes.Element == UInt8 {
        Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    #if canImport(CryptoKit)
    public static func challenge(for verifier: String) -> String {
        base64URL(SHA256.hash(data: Data(verifier.utf8)))
    }

    /// 32 bytes from the system's cryptographically secure generator, which encode to the
    /// 43-character verifier the API's pattern expects.
    public static func generate() -> PKCEPair {
        var generator = SystemRandomNumberGenerator()
        let bytes = (0..<32).map { _ in UInt8.random(in: .min ... .max, using: &generator) }
        let verifier = base64URL(bytes)
        return PKCEPair(verifier: verifier, challenge: challenge(for: verifier))
    }
    #endif

    init(verifier: String, challenge: String) {
        self.verifier = verifier
        self.challenge = challenge
    }
}
