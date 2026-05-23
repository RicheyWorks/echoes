// Workflows list with a one-tap trigger button per workflow.
// (The server doesn't expose a workflow definitions GET today; the
// list comes from the workflow names that have already had at least
// one run. New workflows show up after a register from the CLI.)

import SwiftUI
import AutomatonKit

@MainActor
struct WorkflowsView: View {
    @EnvironmentObject var settings: Settings
    @State private var names: [String] = []
    @State private var error: String?
    @State private var triggerTarget: String?

    var body: some View {
        NavigationStack {
            List {
                if let error = error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
                ForEach(names, id: \.self) { name in
                    HStack {
                        Text(name)
                        Spacer()
                        Button("Trigger") { triggerTarget = name }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                    }
                }
            }
            .refreshable { await refresh() }
            .task { await refresh() }
            .navigationTitle("Workflows")
            .sheet(item: Binding(
                get: { triggerTarget.map { TriggerTarget(name: $0) } },
                set: { triggerTarget = $0?.name }
            )) { target in
                TriggerSheet(name: target.name)
                    .environmentObject(settings)
            }
        }
    }

    private func refresh() async {
        guard let client = settings.makeClient() else {
            error = "Set the server URL + token in Settings."
            return
        }
        do {
            names = try await client.workflows()
            error = nil
        } catch {
            self.error = describe(error)
        }
    }
}

private struct TriggerTarget: Identifiable {
    let name: String
    var id: String { name }
}

@MainActor
private struct TriggerSheet: View {
    let name: String
    @EnvironmentObject var settings: Settings
    @Environment(\.dismiss) private var dismiss
    @State private var payload: String = ""
    @State private var error: String?
    @State private var resultRunId: Int?

    var body: some View {
        NavigationStack {
            Form {
                if let runId = resultRunId {
                    Section { Text("Triggered run #\(runId)") }
                } else {
                    Section("Payload (optional JSON)") {
                        TextEditor(text: $payload)
                            .frame(minHeight: 120)
                            .font(.body.monospaced())
                    }
                }
                if let error = error {
                    Text(error).foregroundStyle(.red)
                }
            }
            .navigationTitle("Trigger \(name)")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
                if resultRunId == nil {
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Trigger") { Task { await trigger() } }
                    }
                }
            }
        }
    }

    private func trigger() async {
        guard let client = settings.makeClient() else {
            error = "Set the server URL + token in Settings."
            return
        }
        var parsed: [String: Any]? = nil
        if !payload.isEmpty {
            guard let data = payload.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                error = "Payload isn't valid JSON."
                return
            }
            parsed = obj
        }
        do {
            resultRunId = try await client.trigger(name, payload: parsed)
        } catch {
            self.error = describe(error)
        }
    }
}
