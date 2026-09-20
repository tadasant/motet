import Foundation
import MotetKit
import Security

/// Where the token lives: the Keychain, not `UserDefaults`.
///
/// The `/v1` bearer token is the only thing standing between the internet and an inference
/// bill (the API's own words). `UserDefaults` is a plist in the app container, readable from
/// an unencrypted backup; the Keychain item below is `ThisDeviceOnly`, so it does not travel
/// in a backup at all.
///
/// The token slot holds a signed-in session and nothing else. Earlier builds also let
/// somebody paste `MOTET_API_TOKEN` here — a non-expiring, owner-equivalent credential on a
/// device that can be lost — and that path is gone (Tadas, 2026-09-19): `reconcile()`
/// removes one left behind by an upgrade.
///
/// The base URL is not a secret and lives in `UserDefaults`. One set under Settings →
/// Advanced wins; without one, a distribution build falls back to `MotetDefaultBaseURL`,
/// which the TestFlight workflow fills from a GitHub environment variable at build time.
/// This repo is public, so the hostname is never written in it. The token has no such
/// default and never will: a token in the binary would be a credential in every copy of it.
@MainActor
final class CredentialStore {
    private let baseURLKey = "motet.baseURL"
    /// Which Google account the stored session belongs to. Not a secret, so not in the
    /// Keychain; nil when nobody is signed in.
    private let signedInEmailKey = "motet.signedInEmail"
    private let tokenAccount = "motet.api-token"
    private let service = "com.getmotet.app"

    var signedInEmail: String? {
        UserDefaults.standard.string(forKey: signedInEmailKey)
    }

    /// Keep a session from signing in. It takes the token's slot: a session is a bearer
    /// token like the API token, so nothing that sends requests knows the difference.
    func saveSession(token: String, email: String) {
        writeToken(token)
        UserDefaults.standard.set(email, forKey: signedInEmailKey)
    }

    func clearSession() {
        writeToken("")
        UserDefaults.standard.removeObject(forKey: signedInEmailKey)
    }

    /// Make the two halves agree at launch, and say whether a pasted token was removed.
    ///
    /// A token with no address is one an earlier build let somebody paste; it is removed,
    /// because the app no longer offers that path and it never expired. An address with no
    /// token is a session that did not survive: the Keychain item is `ThisDeviceOnly` and
    /// `UserDefaults` is not, so a phone restored from a backup arrives with the address and
    /// without the session. Forgetting the address puts the sign-in screen in front, which
    /// is the truth — rather than tabs that silently load nothing.
    ///
    /// **Only a definite answer from the Keychain is acted on.** Before the first unlock
    /// after a restart the item is unreadable rather than absent — it is
    /// `AfterFirstUnlockThisDeviceOnly` — and a launch in that window (a background download
    /// finishing, say) that read "unreadable" as "no session" would sign the person out for
    /// good while their session stayed live on the server.
    @discardableResult
    func reconcile() -> Bool {
        switch tokenState() {
        case .unreadable:
            return false
        case .absent:
            if signedInEmail != nil {
                UserDefaults.standard.removeObject(forKey: signedInEmailKey)
            }
            return false
        case .present:
            guard signedInEmail == nil else { return false }
            writeToken("")
            return true
        }
    }

    /// The server the app talks to: one set under Advanced, else the build's own.
    var serverURL: String { (storedBaseURL() ?? Self.buildDefaultBaseURL)?.absoluteString ?? "" }

    /// The build's own server, offered as "Use the default"; nil on a build that ships none.
    var defaultServerURL: String? { Self.buildDefaultBaseURL?.absoluteString }

    /// Whether `baseURL`, as Advanced would save it, is a different server from the current.
    /// An empty field means the build's own, so saving it where that is current changes
    /// nothing and must not sign anyone out — and neither must a trailing slash or a
    /// capital letter in the host, which name the same server.
    func isDifferentServer(_ baseURL: String) -> Bool {
        Self.canonical(effectiveURL(baseURL)) != Self.canonical(storedBaseURL() ?? Self.buildDefaultBaseURL)
    }

    /// Whether Advanced may save `baseURL`: something with a host, or empty on a build that
    /// has a default of its own. Anything else would sign the person out onto nothing.
    func isValidServer(_ baseURL: String) -> Bool {
        effectiveURL(baseURL)?.host != nil
    }

    /// Point the app at a server. A session belongs to the server that issued it, so a real
    /// change forgets it here — the caller revokes it on the old server first.
    func saveServer(_ baseURL: String) {
        if isDifferentServer(baseURL) {
            clearSession()
        }
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let canonical = Self.canonical(URL(string: trimmed))
        // The build's own is stored as nothing, so a later build pointed elsewhere is not
        // ignored on this phone forever.
        if trimmed.isEmpty || canonical == Self.canonical(Self.buildDefaultBaseURL) {
            UserDefaults.standard.removeObject(forKey: baseURLKey)
        } else {
            UserDefaults.standard.set(canonical ?? trimmed, forKey: baseURLKey)
        }
    }

    /// One spelling per server: scheme and host lowercased, trailing slashes dropped.
    private static func canonical(_ url: URL?) -> String? {
        guard let url, var components = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { return nil }
        components.scheme = components.scheme?.lowercased()
        components.host = components.host?.lowercased()
        while components.path.hasSuffix("/") {
            components.path.removeLast()
        }
        return components.string
    }

    private func effectiveURL(_ raw: String) -> URL? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? Self.buildDefaultBaseURL : URL(string: trimmed)
    }

    func configuration() -> MotetConfiguration {
        MotetConfiguration(baseURL: storedBaseURL() ?? Self.buildDefaultBaseURL, apiToken: readToken())
    }

    private func storedBaseURL() -> URL? {
        guard let stored = UserDefaults.standard.string(forKey: baseURLKey), !stored.isEmpty else {
            return nil
        }
        return URL(string: stored)
    }

    /// HTTPS only, so a mistyped variable cannot point a shipped build at a plaintext host.
    /// The same reason `NSAppTransportSecurity` has no exceptions.
    private static let buildDefaultBaseURL: URL? = {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "MotetDefaultBaseURL") as? String
        else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("https://") else { return nil }
        return URL(string: trimmed)
    }()

    /// The host this build may be handed an https sign-in handoff on, or nil where the
    /// entitlement was not signed in — which is every build but a TestFlight one.
    var appLinkDomain: String? { Self.buildAppLinkDomain }

    /// Which deployment this build was made for, and which server it is on right now.
    ///
    /// Two facts, not one, because they can disagree: the label is compiled in
    /// (`MOTET_BUILD_ENVIRONMENT`), and the server can be moved under Advanced. A staging
    /// build pointed by hand at production would otherwise keep wearing a badge that says
    /// the data in front of you is disposable.
    ///
    /// Neither half is a hostname this repository knows — the label carries no topology at
    /// all, and the host is read off whatever the build or the device was given.
    var buildTarget: BuildTarget {
        let stored = storedBaseURL()
        return BuildTarget(
            environment: Self.buildEnvironment,
            host: BuildTarget.host(of: stored ?? Self.buildDefaultBaseURL),
            isBuildDefault: stored == nil
        )
    }

    private static let buildEnvironment: BuildEnvironment = {
        BuildEnvironment(
            label: Bundle.main.object(forInfoDictionaryKey: "MotetBuildEnvironment") as? String
        )
    }()

    private static let buildAppLinkDomain: String? = {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "MotetAppLinkDomain") as? String
        else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }()

    /// What the Keychain said, kept three ways: "no item" and "could not ask" are different
    /// answers, and only the first is safe to act on (`reconcile()` says why).
    private enum TokenState {
        case present(String)
        case absent
        case unreadable
    }

    private func tokenState() -> TokenState {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: tokenAccount,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return .absent }
        guard status == errSecSuccess,
              let data = item as? Data,
              let token = String(data: data, encoding: .utf8)
        else { return .unreadable }
        return token.isEmpty ? .absent : .present(token)
    }

    private func readToken() -> String? {
        if case .present(let token) = tokenState() { return token }
        return nil
    }

    private func writeToken(_ token: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: tokenAccount,
        ]
        SecItemDelete(query as CFDictionary)
        guard !token.isEmpty else { return }
        var attributes = query
        attributes[kSecValueData as String] = Data(token.utf8)
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(attributes as CFDictionary, nil)
    }
}
