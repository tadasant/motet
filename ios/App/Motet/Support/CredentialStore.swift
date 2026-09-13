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
/// The base URL is not a secret and lives in `UserDefaults`. A URL typed into Settings wins;
/// without one, a distribution build falls back to `MotetDefaultBaseURL`, which the
/// TestFlight workflow fills from a GitHub environment variable at build time. This repo is
/// public, so the hostname is never written in it, and every other build ships the key empty
/// and asks, exactly as before. The token has no such default and never will: a token in
/// the binary would be a credential in every copy of it.
@MainActor
final class CredentialStore {
    private let baseURLKey = "motet.baseURL"
    private let tokenAccount = "motet.api-token"
    private let service = "com.getmotet.app"

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

    func save(baseURL: String, apiToken: String) {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        // Settings shows the build's default, so saving it unchanged must not pin it: a later
        // build pointed somewhere else would otherwise be ignored on this phone forever.
        if trimmed == Self.buildDefaultBaseURL?.absoluteString {
            UserDefaults.standard.removeObject(forKey: baseURLKey)
        } else {
            UserDefaults.standard.set(trimmed, forKey: baseURLKey)
        }
        writeToken(apiToken.trimmingCharacters(in: .whitespacesAndNewlines))
    }

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
