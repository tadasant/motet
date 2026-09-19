import AuthenticationServices
import MotetKit
import os
import SwiftUI

/// The door: nothing else in the app renders until this has signed somebody in.
///
/// Asked for by Tadas on 2026-09-19 ("google login should happen first"). The app used to
/// open straight into its tabs, with Google sign-in offered on the Settings tab beside a
/// pasted API token; now the web sign-in is the only way in (AGENTS.md, "The phone signs in
/// through the web sign-in"), and the server is the build's own unless changed under
/// Advanced.
struct SignInView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    @State private var server = ""
    @State private var showingAdvanced = false

    private static let logger = Logger(subsystem: "com.getmotet.app", category: "signin")

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                Spacer(minLength: 56)
                VStack(alignment: .leading, spacing: 14) {
                    VoiceDots(size: 8)
                    Wordmark(size: 44)
                    Text("Your reading backlog, as a podcast you can talk back to.")
                        .font(Theme.display(22))
                        .foregroundStyle(Theme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                VStack(alignment: .leading, spacing: 12) {
                    Button(model.isSigningIn ? "Signing in…" : "Sign in with Google") {
                        Task { await signIn() }
                    }
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(model.isSigningIn || !model.isValidServer(server))
                    if let message = model.signInMessage {
                        Text(message)
                            .font(Theme.aside(15))
                            .foregroundStyle(Theme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Text("Only accounts this Motet allows can sign in. The session is kept in the Keychain, on this device only.")
                        .font(Theme.aside(14))
                        .foregroundStyle(Theme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
                DisclosureGroup("Advanced", isExpanded: $showingAdvanced) {
                    ServerField(server: $server, defaultServer: model.defaultServerURL)
                        .textFieldStyle(.roundedBorder)
                        .padding(.top, 10)
                }
                .font(Theme.body(15, weight: 500))
                .foregroundStyle(Theme.ink)
            }
            .padding(.horizontal, 28)
            .padding(.bottom, 32)
        }
        .background(Theme.parchment.ignoresSafeArea())
        .onAppear {
            server = model.serverURL
            // A build with no server of its own (anything but TestFlight) needs one typed in
            // before the button can work, so the place to type it starts open.
            if server.isEmpty { showingAdvanced = true }
        }
    }

    private var trimmedServer: String {
        server.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// How one presentation of the sign-in sheet ended.
    private enum SheetResult {
        case signedIn(URL)
        /// A person dismissed it. Nothing to say.
        case dismissed
        /// It was refused, or failed, before anyone could use it.
        case failed
    }

    /// Sign in, falling back to the custom scheme if the https callback is refused.
    ///
    /// The https callback can be refused when the entitlement is signed in but not yet in
    /// force on this device — iOS checks the domain association when the app is installed
    /// or updated, so a build installed before the web app served the file stays unverified
    /// until the next install. The refusal happens as the sheet opens, before anything has
    /// happened, so a fresh sign-in on the scheme costs one round trip. It has to be a
    /// fresh one: the server already committed the first to the https shape.
    private func signIn() async {
        let server = trimmedServer
        guard let started = await model.beginSignIn(baseURL: server) else { return }
        switch await present(started) {
        case .signedIn(let callback):
            await model.finishSignIn(
                callback: callback, pkce: started.pkce, baseURL: server, appLinkHost: started.appLinkHost
            )
        case .dismissed:
            model.abandonSignIn(nil)
        case .failed:
            guard started.appLinkHost != nil else {
                model.signInWindowFailed()
                return
            }
            Self.logger.notice("https sign-in callback refused; retrying on the custom scheme")
            // Let the refused session finish tearing down, so the next presentation is not
            // refused for overlapping it.
            try? await Task.sleep(for: .milliseconds(400))
            await signInWithTheScheme(server: server)
        }
    }

    /// Second attempt, with the custom scheme both sides always support.
    private func signInWithTheScheme(server: String) async {
        guard let started = await model.beginSignIn(baseURL: server, allowAppLink: false) else {
            return
        }
        switch await present(started) {
        case .signedIn(let callback):
            await model.finishSignIn(callback: callback, pkce: started.pkce, baseURL: server)
        case .dismissed:
            model.abandonSignIn(nil)
        case .failed:
            model.signInWindowFailed()
        }
    }

    /// Show the sheet once, and say how it ended — telling a person's Cancel from a refusal
    /// that iOS reports with the same error code.
    private func present(_ started: AppModel.StartedSignIn) async -> SheetResult {
        let clock = ContinuousClock()
        let opened = clock.now
        do {
            return .signedIn(try await open(started))
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            let elapsed = opened.duration(to: clock.now)
            switch NativeSignIn.classifyCancellation(after: elapsed) {
            case .dismissedByPerson:
                // Logged too, so a threshold that is wrong for some device shows up as a run of
                // these rather than as the silence this code exists to end.
                Self.logger.notice(
                    "sign-in sheet dismissed \(String(describing: elapsed), privacy: .public) after opening"
                )
                return .dismissed
            case .refusedBeforeShown:
                Self.logger.error(
                    "sign-in sheet reported a cancellation \(String(describing: elapsed), privacy: .public) after opening; treating it as refused: \(error.localizedDescription, privacy: .public)"
                )
                return .failed
            }
        } catch is CancellationError {
            return .dismissed
        } catch {
            Self.logger.error("sign-in sheet failed: \(error.localizedDescription, privacy: .public)")
            return .failed
        }
    }

    /// Open the sheet, waiting for whichever callback this sign-in was started for.
    ///
    /// The https callback needs iOS 17.4, an entitlement for the host, and an
    /// app-site-association file this device has verified. `beginSignIn` has already
    /// agreed which one with the server, so this only carries out that decision.
    private func open(_ started: AppModel.StartedSignIn) async throws -> URL {
        if #available(iOS 17.4, *), let host = started.appLinkHost, let path = started.appLinkPath {
            // `additionalHeaderFields` is spelled out because the overload taking a
            // `callback:` declares no default for it, unlike the `callbackURLScheme:` one
            // below. Empty: the sign-in is a plain web sign-in and needs no extra headers.
            return try await webAuthenticationSession.authenticate(
                using: started.url,
                callback: .https(host: host, path: path),
                additionalHeaderFields: [:]
            )
        }
        return try await webAuthenticationSession.authenticate(
            using: started.url, callbackURLScheme: started.callbackScheme
        )
    }
}

/// The server URL under Advanced: the build's own unless changed here.
///
/// The default is read from the build rather than written in this file, because this repo is
/// public and names no deployment's host (`CredentialStore` says where it comes from).
struct ServerField: View {
    @Binding var server: String
    let defaultServer: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("https://…", text: $server)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .textContentType(.URL)
            if let defaultServer,
               server.trimmingCharacters(in: .whitespacesAndNewlines) != defaultServer {
                Button("Use the default (\(defaultServer))") { server = defaultServer }
                    .font(Theme.body(14, weight: 500))
            }
        }
    }
}
