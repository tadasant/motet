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
/// The base URL is not a secret and lives in `UserDefaults` — but it is still *typed in*
/// rather than compiled in, because this repo is public and a hostname in it is
/// infrastructure topology.
@MainActor
final class CredentialStore {
    private let baseURLKey = "motet.baseURL"
    private let tokenAccount = "motet.api-token"
    private let service = "com.getmotet.app"

    func configuration() -> MotetConfiguration {
        MotetConfiguration(
            baseURL: UserDefaults.standard.string(forKey: baseURLKey).flatMap(URL.init(string:)),
            apiToken: readToken()
        )
    }

    func save(baseURL: String, apiToken: String) {
        let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(trimmed, forKey: baseURLKey)
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
