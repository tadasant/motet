import MotetKit
import SwiftUI

/// Where the app is pointed, and how it behaves on a walk.
///
/// The server URL and token are typed in here rather than compiled in: this repo is public,
/// so a default hostname would be infrastructure topology in it and a default token would be
/// a credential in a shipped binary.
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var baseURL = ""
    @State private var apiToken = ""
    @State private var offlineBytes = 0

    var body: some View {
        NavigationStack {
            Form {
                serverSection
                listeningSection
                offlineSection
            }
            .navigationTitle("Settings")
            .task {
                let current = model.currentCredentials()
                baseURL = current.baseURL
                apiToken = current.apiToken
                offlineBytes = (try? await model.library.offlineBytes()) ?? 0
            }
        }
    }

    private var serverSection: some View {
        Section {
            TextField("https://…", text: $baseURL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            SecureField("API token", text: $apiToken)
            Button("Save and refresh") {
                Task { await model.saveCredentials(baseURL: baseURL, apiToken: apiToken) }
            }
            .disabled(baseURL.trimmingCharacters(in: .whitespaces).isEmpty)
        } header: {
            Text("Server")
        } footer: {
            Text("The token is kept in the Keychain, on this device only.")
        }
    }

    private var listeningSection: some View {
        Section("Listening") {
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
        }
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
            Text("Offline")
        } footer: {
            Text("Downloaded before you leave, so a walk with no signal still plays.")
        }
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
