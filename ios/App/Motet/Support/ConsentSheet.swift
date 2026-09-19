import AuthenticationServices
import MotetKit
import os
import SwiftUI

/// The system sign-in sheet, opened on a provider's consent page and told to finish on
/// `motet://consent`, which the web app's `/oauth/callback` page hands the consent to (see
/// `ConsentCallback` for why the sheet no longer waits for Google's https redirect itself).
///
/// Separate from `SignInView`'s presentation because what happens on a refusal differs: a
/// consent has no second shape to fall back to, so a refusal here sends the person to the
/// web app instead.
enum ConsentSheet {
    typealias Result = ConsentFlow.Presentation

    private static let logger = Logger(subsystem: "com.getmotet.app", category: "consent")

    /// Whether this build can receive a consent at all: it has to know its web app, whose
    /// callback page hands the consent back. Any iOS the app runs on can catch a custom
    /// scheme, and no associated-domain verification is involved.
    static func isAvailable(appDomain: String?) -> Bool {
        ConsentCallback.redirectURI(appDomain: appDomain) != nil
    }

    @MainActor
    static func present(
        _ url: URL, appDomain: String?, using session: WebAuthenticationSession
    ) async -> Result {
        guard isAvailable(appDomain: appDomain) else { return .refused }
        let clock = ContinuousClock()
        let opened = clock.now
        do {
            let callback = try await session.authenticate(
                using: url, callbackURLScheme: ConsentCallback.callbackScheme
            )
            return .callback(callback)
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            // iOS reports a refusal to show the sheet with the same code as a person's
            // Cancel. Nobody dismisses a consent page inside a second, so that is a refusal
            // (`NativeSignIn`).
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
