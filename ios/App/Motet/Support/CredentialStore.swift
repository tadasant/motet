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
    /// Which Google account the stored token is a session for. Not a secret, so not in the
    /// Keychain; nil when the token was pasted rather than signed in for.
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
    @discardableResult
    func reconcile() -> Bool {
        let hasToken = !(readToken() ?? "").isEmpty
        if signedInEmail != nil, !hasToken {
            UserDefaults.standard.removeObject(forKey: signedInEmailKey)
        }
        guard signedInEmail == nil, hasToken else { return false }
        writeToken("")
        return true
    }

    /// The server the app talks to: one set under Advanced, else the build's own.
    var serverURL: String { (storedBaseURL() ?? Self.buildDefaultBaseURL)?.absoluteString ?? "" }

    /// The build's own server, offered as "Use the default"; nil on a build that ships none.
    var defaultServerURL: String? { Self.buildDefaultBaseURL?.absoluteString }

    /// Whether `baseURL`, as Advanced would save it, is a different server from the current.
    /// An empty field means the build's own, so saving it where that is current changes
    /// nothing and must not sign anyone out.
    func isDifferentServer(_ baseURL: String) -> Bool {
        effectiveURL(baseURL) != (storedBaseURL() ?? Self.buildDefaultBaseURL)
    }

    /// Point the app at a server. A session belongs to the server that issued it, so a real
    /// change forgets it here — the caller revokes it on the old server first.
    func saveServer(_ baseURL: String) {
        if isDifferentServer(baseURL) {
            clearSession()
        }
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        // The build's own is stored as nothing, so a later build pointed elsewhere is not
        // ignored on this phone forever.
        if trimmed.isEmpty || trimmed == Self.buildDefaultBaseURL?.absoluteString {
            UserDefaults.standard.removeObject(forKey: baseURLKey)
        } else {
            UserDefaults.standard.set(trimmed, forKey: baseURLKey)
        }
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

    private static let buildAppLinkDomain: String? = {
        guard let raw = Bundle.main.object(forInfoDictionaryKey: "MotetAppLinkDomain") as? String
        else { return nil }
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }()

    private func readToken() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: tokenAccount,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
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
