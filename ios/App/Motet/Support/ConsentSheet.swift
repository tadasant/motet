import AuthenticationServices
import MotetKit
import os
import SwiftUI

/// The system sign-in sheet, opened on a provider's consent page and told to finish on the
/// web app's own `/oauth/callback` (see `ConsentCallback` for why that is the only address
/// that works, and why the SPA never sees the code).
///
/// Separate from `SignInView`'s presentation because what happens on a refusal differs: a
/// sign-in falls back to the custom scheme, which the API can hand back, while a consent
/// has nowhere else to come back to — Google returns to the one registered web address —
/// so a refusal here sends the person to the web app instead.
enum ConsentSheet {
    enum Result {
        case callback(URL)
        /// A person dismissed it. Nothing to say.
        case dismissed
        /// iOS would not show it, or it failed before anyone could use it.
        case refused
    }

    private static let logger = Logger(subsystem: "com.getmotet.app", category: "consent")

    /// Whether this build and this iOS can receive a consent at all.
    static func isAvailable(appDomain: String?) -> Bool {
        guard ConsentCallback.redirectURI(appDomain: appDomain) != nil else { return false }
        if #available(iOS 17.4, *) { return true }
        return false
    }

    @MainActor
    static func present(
        _ url: URL, appDomain: String?, using session: WebAuthenticationSession
    ) async -> Result {
        guard let host = ConsentCallback.redirectURI(appDomain: appDomain)
            .flatMap(URL.init(string:))?.host
        else { return .refused }
        guard #available(iOS 17.4, *) else { return .refused }
        let clock = ContinuousClock()
        let opened = clock.now
        do {
            let callback = try await session.authenticate(
                using: url,
                callback: .https(host: host, path: ConsentCallback.callbackPath),
                additionalHeaderFields: [:]
            )
            return .callback(callback)
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            // iOS reports a refusal to show the sheet — an https callback on a domain this
            // device has not verified — with the same code as a person's Cancel. Nobody
            // dismisses a consent page inside a second, so that is a refusal (`NativeSignIn`).
            let elapsed = opened.duration(to: clock.now)
            switch NativeSignIn.classifyCancellation(after: elapsed) {
            case .dismissedByPerson:
                return .dismissed
            case .refusedBeforeShown:
                logger.error(
                    "consent sheet cancelled \(String(describing: elapsed), privacy: .public) after opening; treating it as refused"
                )
                return .refused
            }
        } catch is CancellationError {
            return .dismissed
        } catch {
            logger.error("consent sheet failed: \(error.localizedDescription, privacy: .public)")
            return .refused
        }
    }
}
