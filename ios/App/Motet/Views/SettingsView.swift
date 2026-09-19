import MotetKit
import SwiftUI

/// Who the app is signed in as, which server it talks to, and how it behaves on a walk.
///
/// Signing in happens on `SignInView`, which is all that renders until a session exists
/// (AGENTS.md, "The phone signs in through the web sign-in"). There is no pasted API token
/// any more (Tadas, 2026-09-19); the server is the build's own unless changed under
/// Advanced, and changing it signs the phone out, because a session belongs to the server
/// that issued it.
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var server = ""
    @State private var showingAdvanced = false
    @State private var offlineBytes = 0

    var body: some View {
        NavigationStack {
            Form {
                accountSection
                listeningSection
                offlineSection
                advancedSection
            }
            .brandGround()
            .foregroundStyle(Theme.ink)
            .font(Theme.body(16))
            .navigationTitle("Settings")
            .task {
                server = model.serverURL
                offlineBytes = (try? await model.library.offlineBytes()) ?? 0
            }
        }
    }

    private var accountSection: some View {
        Section {
            LabeledContent("Signed in as", value: model.signedInEmail ?? "—")
                .listRowBackground(Theme.surface)
            Button("Sign out", role: .destructive) {
                Task { await model.signOut() }
            }
            .listRowBackground(Theme.surface)
        } header: {
            Text("Account").brandLabel()
        } footer: {
            Text("The session is kept in the Keychain, on this device only. Signing out revokes it on the server too.")
                .font(Theme.aside(14))
                .foregroundStyle(Theme.inkSoft)
        }
    }

    /// Last, and folded: the server is set once by the build and almost never changed.
    private var advancedSection: some View {
        Section {
            DisclosureGroup("Advanced", isExpanded: $showingAdvanced) {
                ServerField(server: $server, defaultServer: model.defaultServerURL)
                Button("Save server") {
                    Task {
                        await model.saveServer(server)
                        server = model.serverURL
                    }
                }
                .disabled(!model.isValidServer(server) || !model.isDifferentServer(server))
            }
            .listRowBackground(Theme.surface)
        } footer: {
            Text("The server this app talks to. Changing it signs you out, because a session belongs to the server that issued it.")
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
