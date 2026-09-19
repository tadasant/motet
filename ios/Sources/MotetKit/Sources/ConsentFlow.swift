import Foundation

/// One consent from start to finish: ask the API for the provider's page, show it, read what
/// came back, and finish at the API — with every way that can end said in one sentence.
///
/// Kept in MotetKit, apart from the sheet that shows the page, so each ending is tested on
/// Linux: a person's Cancel, a sheet iOS refused, the provider's `access_denied`, a callback
/// for some other attempt, and a finish the API refused.
public enum ConsentFlow {
    /// How the sheet ended. The app's `ConsentSheet` produces one of these.
    public enum Presentation: Equatable, Sendable {
        case callback(URL)
        /// A person dismissed it.
        case dismissed
        /// iOS would not show it, or it failed before anyone could use it.
        case refused
    }

    /// What `start` produced: the provider's page, the `state` it carries, and — for a
    /// consent that created a row to attach to — that row, so a consent nobody ever saw can
    /// take it away again.
    public struct Started: Sendable {
        public let url: String
        public let state: String
        public let createdSourceId: String?

        public init(url: String, state: String, createdSourceId: String? = nil) {
            self.url = url
            self.state = state
            self.createdSourceId = createdSourceId
        }
    }

    public enum Outcome: Equatable, Sendable {
        /// Finished. The sentence says what changed.
        case finished(String)
        /// Did not finish. The sentence says why, and that nothing changed where true.
        case notFinished(String)
        /// The sheet could not be shown here; only the web app can take this consent.
        case needsWebApp
    }

    /// Runs on the caller's actor (`#isolation`), so the closures it is handed — which in
    /// the app belong to the main actor — are never sent anywhere.
    public static func run(
        isolation: isolated (any Actor)? = #isolation,
        api: any SourcesAPI,
        appDomain: String?,
        what: String,
        start: (any SourcesAPI) async throws -> Started,
        present: (URL) async -> Presentation,
        finish: (any SourcesAPI, _ code: String, _ state: String, _ iss: String?) async throws -> String
    ) async -> Outcome {
        let started: Started
        do {
            started = try await start(api)
        } catch {
            return .notFinished(describe(error))
        }
        guard let url = URL(string: started.url) else {
            return .notFinished("The API answered with a consent address that is not a URL.")
        }
        switch await present(url) {
        case .dismissed:
            return .notFinished("You closed the consent page before finishing. Nothing was changed.")
        case .refused:
            // Nobody saw the provider's page, so the row `connect` made for it is an attempt
            // nobody made. Left, it would read "Awaiting consent" — once per retry.
            if let id = started.createdSourceId {
                try? await api.removeSource(id: id)
            }
            return .needsWebApp
        case .callback(let callback):
            do {
                switch try ConsentCallback.outcome(from: callback, appDomain: appDomain, expectedState: started.state) {
                case .denied(let error, let description):
                    return .notFinished(ConsentCallback.describeDenial(error: error, description: description, what: what))
                case .granted(let code, let state, let iss):
                    return .finished(try await finish(api, code, state, iss))
                }
            } catch {
                return .notFinished(describe(error))
            }
        }
    }

    /// A refusal in the API's own words, and a 503 said to be configuration.
    public static func describe(_ error: Error) -> String {
        if let error = error as? MotetError {
            switch error {
            case .http(503, let detail?):
                return "This deployment can’t do that right now. That is configuration, not you: \(detail)"
            case .http(_, let detail?):
                return detail
            default:
                return error.description
            }
        }
        if let error = error as? ConsentCallback.Failure { return error.description }
        return error.localizedDescription
    }
}
