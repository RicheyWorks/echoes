// Settings: server URL + bearer token + optional self-signed cert pin.

import SwiftUI
import AutomatonKit

@MainActor
struct SettingsView: View {
    @EnvironmentObject var settings: Settings
    @State private var token: String = ""
    @State private var lastTestResult: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("https://automaton.your-tailnet.ts.net:8443",
                                text: $settings.serverURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                }
                Section("Bearer token") {
                    SecureField(settings.hasToken ? "(stored in Keychain)" : "paste here",
                                text: $token)
                    HStack {
                        Button(settings.hasToken ? "Replace" : "Save") {
                            settings.saveToken(token)
                            token = ""
                        }
                        if settings.hasToken {
                            Spacer()
                            Button("Clear", role: .destructive) {
                                settings.saveToken("")
                            }
                        }
                    }
                }
                Section("Self-signed cert pin (optional)") {
                    TextField("sha256 hex, no colons",
                                text: $settings.pinnedFingerprint)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .font(.body.monospaced())
                    Text("Set this when using the cert from `automaton tls init` and you don't want to trust the cert system-wide.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
                Section {
                    Button("Test connection") { Task { await test() } }
                    if let r = lastTestResult { Text(r).font(.footnote) }
                }
            }
            .navigationTitle("Settings")
        }
    }

    private func test() async {
        guard let client = settings.makeClient() else {
            lastTestResult = "Set both server URL and token first."
            return
        }
        do {
            let ok = try await client.health()
            lastTestResult = ok ? "Connection OK" : "Server returned not-OK health."
        } catch {
            lastTestResult = describe(error)
        }
    }
}

extension Settings {
    /// Build a client from the current settings, or nil if URL/token aren't set.
    func makeClient() -> AutomatonClient? {
        guard let url = URL(string: serverURL), !serverURL.isEmpty else {
            return nil
        }
        let token = loadToken()
        return AutomatonClient(
            baseURL: url,
            token: token,
            pinnedCertSHA256: pinnedFingerprint.isEmpty ? nil : pinnedFingerprint
        )
    }
}
