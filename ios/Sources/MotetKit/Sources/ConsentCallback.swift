import Foundation

/// Connecting a mailbox, re-authorizing it, or authorizing an MCP server from the phone —
/// and what came back.
///
/// **No new redirect URI exists for the app, and none is needed.** Google returns every
/// consent to the one address registered on Motet's OAuth client: the web app's
/// `/oauth/callback` (AGENTS.md, "Gmail is the seam to the mailbox"). Registering a second
/// one is a human step under invariant 9, and an iOS-type client would be a second OAuth
/// client besides. So the app asks for that same web address and opens the consent in the
/// system sign-in sheet, told to finish on an **https callback for the web app's own host
/// and path** — the shared-web-credentials association the phone's sign-in already relies
/// on ("The handoff comes back on a verified https link"). iOS closes the sheet the moment
/// Google redirects there and hands the URL to the app, *before the page loads*, so the SPA
/// never sees the code and the app finishes the consent with its own session, at the same
/// route the SPA would have called.
///
/// Where that association is not in force — a build with no `MotetAppLinkDomain`, an iOS
/// older than 17.4, or a device that has not verified the domain yet — there is no way for
/// the consent to come back to the app, and the screen says so and offers the web app
/// instead. A custom-scheme redirect would need a new registration on Google's side and
/// Google refuses custom schemes on a web client anyway.
public enum ConsentCallback {
    /// The path the web app serves the callback on. Keep in step with `CALLBACK_PATH` in
    /// `web/src/oauth.ts` and `motet_api`: it is the registered string, and it must not drift.
    public static let callbackPath = "/oauth/callback"

    /// The redirect URI to hand the API: the web app's registered callback.
    ///
    /// `appDomain` is the build's `MotetAppLinkDomain` — the web app's bare host, which is
    /// what `MOTET_IOS_APP_DOMAIN` holds. Nil or empty means this build cannot receive a
    /// consent at all.
    public static func redirectURI(appDomain: String?) -> String? {
        guard let host = appDomain?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
              !host.isEmpty, !host.contains("/"), !host.contains(":"), !host.contains("@")
        else { return nil }
        return "https://\(host)\(callbackPath)"
    }

    /// Where to send a person whose phone cannot finish a consent itself.
    public static func webSourcesURL(appDomain: String?) -> URL? {
        guard let redirect = redirectURI(appDomain: appDomain),
              var components = URLComponents(string: redirect)
        else { return nil }
        components.path = "/sources"
        return components.url
    }

    /// What Google put in the query string when it sent the person back.
    public enum Outcome: Equatable, Sendable {
        /// `iss` is RFC 9207's issuer identifier: an MCP authorization server that supports
        /// it sends one, and the connector callback checks it. Google does not.
        case granted(code: String, state: String, iss: String?)
        /// The person said no, or the provider refused. `error` is its own code.
        case denied(error: String, description: String)
    }

    public enum Failure: Error, Equatable, CustomStringConvertible {
        /// The sheet came back somewhere that is not the callback it was opened for.
        case notTheCallback
        /// A callback carrying neither a code nor an error.
        case empty
        /// The `state` is not the one this consent started with — a callback for some
        /// other authorization. The API would refuse it too; this saves spending it.
        case stateMismatch

        public var description: String {
            switch self {
            case .notTheCallback: return "The consent came back somewhere unexpected. Try again."
            case .empty: return "The consent came back with nothing in it. Try again."
            case .stateMismatch: return "That consent belongs to a different attempt. Try again."
            }
        }
    }

    /// Read the callback URL the sheet returned.
    ///
    /// `expectedState` is the state the API minted for this consent. The host is checked as
    /// well as the path: in practice the system hands back only the host the sheet was told
    /// to wait for, but this is a public entry point and `NativeSignIn` checks its host too.
    public static func outcome(
        from url: URL, appDomain: String?, expectedState: String
    ) throws -> Outcome {
        guard let redirect = redirectURI(appDomain: appDomain).flatMap(URL.init(string:)),
              url.scheme?.lowercased() == "https",
              url.host?.lowercased() == redirect.host,
              url.path.trimmingTrailingSlashes == callbackPath,
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { throw Failure.notTheCallback }
        let items = components.queryItems ?? []
        func value(_ name: String) -> String? {
            items.first(where: { $0.name == name })?.value.flatMap { $0.isEmpty ? nil : $0 }
        }
        let state = value("state")
        if let state, state != expectedState { throw Failure.stateMismatch }
        if let error = value("error") {
            return .denied(error: error, description: value("error_description") ?? "")
        }
        guard let code = value("code"), let state else { throw Failure.empty }
        return .granted(code: code, state: state, iss: value("iss"))
    }

    /// What a refused consent means, in a sentence. `access_denied` is somebody pressing
    /// Cancel on the provider's page — a supported answer, not a failure.
    public static func describeDenial(error: String, description: String, what: String) -> String {
        if error == "access_denied" {
            return "You didn’t grant access to \(what). Nothing was changed."
        }
        let detail = description.isEmpty ? error : "\(error): \(description)"
        return "The provider refused (\(detail)). Nothing was changed."
    }
}

extension String {
    fileprivate var trimmingTrailingSlashes: String {
        var copy = self
        while copy.count > 1, copy.hasSuffix("/") { copy.removeLast() }
        return copy
    }
}
