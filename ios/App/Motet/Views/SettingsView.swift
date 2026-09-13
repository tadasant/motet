import AuthenticationServices
import MotetKit
import SwiftUI

/// Where the app is pointed, who it is signed in as, and how it behaves on a walk.
///
/// Signing in goes through the web sign-in in the system sheet and ends in a session this
/// device keeps (AGENTS.md, "The phone signs in through the web sign-in"). An API token can
/// still be pasted instead; it is never compiled in, because a default token would be a
/// credential in a shipped binary. The server URL arrives prefilled on a TestFlight build
/// (`CredentialStore` says where from) and is typed in on every other build.
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.webAuthenticationSession) private var webAuthenticationSession
    @State private var baseURL = ""
    @State private var apiToken = ""
    @State private var offlineBytes = 0

    var body: some View {
        NavigationStack {
            Form {
                accountSection
                serverSection
                listeningSection
                offlineSection
            }
            .brandGround()
            .foregroundStyle(Theme.ink)
            .font(Theme.body(16))
            .navigationTitle("Settings")
            .task {
                let current = model.currentCredentials()
                baseURL = current.baseURL
                apiToken = current.apiToken
                offlineBytes = (try? await model.library.offlineBytes()) ?? 0
            }
        }
    }

    private var accountSection: some View {
        Section {
            if let email = model.signedInEmail {
                LabeledContent("Signed in as", value: email)
                    .listRowBackground(Theme.surface)
                Button("Sign out", role: .destructive) {
                    Task {
                        await model.signOut()
                        apiToken = model.currentCredentials().apiToken
                    }
                }
                .listRowBackground(Theme.surface)
            } else {
                Button(model.isSigningIn ? "Signing in…" : "Sign in with Google") {
                    Task { await signIn() }
                }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(model.isSigningIn || baseURL.trimmingCharacters(in: .whitespaces).isEmpty)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 12, leading: 0, bottom: 4, trailing: 0))
            }
            if let message = model.signInMessage {
                Text(message)
                    .font(Theme.aside(14))
                    .foregroundStyle(Theme.inkSoft)
                    .listRowBackground(Color.clear)
            }
        } header: {
            Text("Account").brandLabel()
        } footer: {
            Text("Signs in with a Google account this Motet allows. The session is kept in the Keychain, on this device only.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
    }

    /// The system sheet opens the web sign-in and closes on the API's handoff link.
    private func signIn() async {
        let server = baseURL
        guard let started = await model.beginSignIn(baseURL: server) else { return }
        do {
            let callback = try await open(started)
            await model.finishSignIn(
                callback: callback,
                pkce: started.pkce,
                baseURL: server,
                appLinkHost: started.appLinkHost
            )
            apiToken = model.currentCredentials().apiToken
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            model.abandonSignIn(nil)
        } catch is CancellationError {
            model.abandonSignIn(nil)
        } catch {
            if started.appLinkHost != nil {
                // The https callback is refused when the entitlement is not in force — the
                // association file not fetched yet, a build that lost it at export, the
                // capability not ticked on the App ID — and it is refused when the sheet
                // *opens*, so nothing has happened yet and retrying costs one round trip.
                // The server committed to the https link for this sign-in, so the retry has
                // to start a new one rather than reuse it.
                await signInWithTheScheme(server: server)
                return
            }
            model.abandonSignIn(error)
        }
    }

    /// Second attempt, with the custom scheme both sides always support.
    private func signInWithTheScheme(server: String) async {
        guard let started = await model.beginSignIn(baseURL: server, allowAppLink: false) else {
            return
        }
        do {
            let callback = try await webAuthenticationSession.authenticate(
                using: started.url, callbackURLScheme: started.callbackScheme
            )
            await model.finishSignIn(callback: callback, pkce: started.pkce, baseURL: server)
            apiToken = model.currentCredentials().apiToken
        } catch let error as ASWebAuthenticationSessionError where error.code == .canceledLogin {
            model.abandonSignIn(nil)
        } catch is CancellationError {
            model.abandonSignIn(nil)
        } catch {
            model.abandonSignIn(error)
        }
    }

    /// Open the sheet, waiting for whichever callback this sign-in was started for.
    ///
    /// The https callback needs iOS 17.4, an entitlement for the host, and an
    /// app-site-association file Apple has fetched from it. `beginSignIn` has already
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

    private var serverSection: some View {
        Section {
            TextField("https://…", text: $baseURL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
                .listRowBackground(Theme.surface)
            DisclosureGroup("Use an API token instead") {
                SecureField("API token", text: $apiToken)
            }
            .listRowBackground(Theme.surface)
            Button("Save and refresh") {
                Task { await model.saveCredentials(baseURL: baseURL, apiToken: apiToken) }
            }
            .buttonStyle(PrimaryButtonStyle())
            .disabled(baseURL.trimmingCharacters(in: .whitespaces).isEmpty)
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 12, leading: 0, bottom: 4, trailing: 0))
        } header: {
            Text("Server").brandLabel()
        } footer: {
            Text("The token is kept in the Keychain, on this device only.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
    }

    private var listeningSection: some View {
        Section {
            Picker("Speed", selection: rateBinding) {
                ForEach(PlaybackSettings.rateLadder, id: \.self) { rate in
                    Text(Format.rate(rate)).tag(rate)
                }
            }
            Stepper(
                "Forward \(model.settings.skipForwardMs / 1_000)s",
                value: skipForwardSecondsBinding,
                in: 5...120,
                step: 5
            )
            Stepper(
                "Back \(model.settings.skipBackwardMs / 1_000)s",
                value: skipBackwardSecondsBinding,
                in: 5...120,
                step: 5
            )
        } header: {
            Text("Listening").brandLabel()
        }
        .listRowBackground(Theme.surface)
        .monospacedDigit()
    }

    private var offlineSection: some View {
        Section {
            Stepper(
                "Keep \(model.settings.episodesToKeepOffline) episodes",
                value: episodesToKeepBinding,
                in: 0...20
            )
            LabeledContent("On this device", value: Format.bytes(offlineBytes))
        } header: {
            Text("Offline").brandLabel()
        } footer: {
            Text("Downloaded before you leave, so a walk with no signal still plays.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
        .listRowBackground(Theme.surface)
        .monospacedDigit()
    }

    // MARK: - Bindings

    // `PlaybackSettings` is a value with a validating initialiser, so each control replaces
    // the whole thing rather than mutating a field — which is also what makes "save" a
    // single, persisted write.

    private var rateBinding: Binding<Double> {
        Binding(
            get: { model.settings.rate },
            set: { newValue in save(rate: newValue) }
        )
    }

    private var skipForwardSecondsBinding: Binding<Int> {
        Binding(
            get: { model.settings.skipForwardMs / 1_000 },
            set: { seconds in save(skipForwardMs: seconds * 1_000) }
        )
    }

    private var skipBackwardSecondsBinding: Binding<Int> {
        Binding(
            get: { model.settings.skipBackwardMs / 1_000 },
            set: { seconds in save(skipBackwardMs: seconds * 1_000) }
        )
    }

    private var episodesToKeepBinding: Binding<Int> {
        Binding(
            get: { model.settings.episodesToKeepOffline },
            set: { count in save(episodesToKeepOffline: count) }
        )
    }

    private func save(
        rate: Double? = nil,
        skipForwardMs: Int? = nil,
        skipBackwardMs: Int? = nil,
        episodesToKeepOffline: Int? = nil
    ) {
        let current = model.settings
        let updated = PlaybackSettings(
            rate: rate ?? current.rate,
            skipForwardMs: skipForwardMs ?? current.skipForwardMs,
            skipBackwardMs: skipBackwardMs ?? current.skipBackwardMs,
            episodesToKeepOffline: episodesToKeepOffline ?? current.episodesToKeepOffline
        )
        Task { await model.updateSettings(updated) }
    }
}
