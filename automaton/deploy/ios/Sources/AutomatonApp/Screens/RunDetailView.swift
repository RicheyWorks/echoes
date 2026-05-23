// Run detail: step tree + event log + live updates via polling.
//
// SSE would be cleaner, but URLSession doesn't ship with a native SSE
// reader; for personal-infra use cases the 2 s poll is plenty.

import SwiftUI
import AutomatonKit

@MainActor
struct RunDetailView: View {
    let runId: Int
    @EnvironmentObject var settings: Settings
    @State private var detail: RunDetail?
    @State private var error: String?
    @State private var pollTask: Task<Void, Never>?
    @State private var showSignalSheet = false

    var body: some View {
        List {
            if let detail = detail {
                Section("Run") {
                    LabeledContent("ID", value: "#\(detail.run.id)")
                    LabeledContent("Workflow",
                                    value: detail.run.workflow ?? "?")
                    LabeledContent("Status",
                                    value: detail.run.status.rawValue)
                }
                Section("Steps") {
                    ForEach(detail.steps) { s in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text("\(s.name) #\(s.attempt)")
                                    .font(.body.weight(.medium))
                                Spacer()
                                Text(s.status).font(.caption)
                            }
                            if let out = s.outputJson {
                                Text(out).font(.caption.monospaced())
                                    .foregroundStyle(.secondary)
                                    .lineLimit(8)
                            }
                            if let err = s.errorJson {
                                Text(err).font(.caption.monospaced())
                                    .foregroundStyle(.red)
                                    .lineLimit(8)
                            }
                        }
                    }
                }
                Section("Event log") {
                    ForEach(detail.events) { e in
                        VStack(alignment: .leading) {
                            HStack {
                                Text(e.kind).font(.caption.weight(.medium))
                                Spacer()
                                Text(e.ts).font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                            if let payload = e.payloadJson, !payload.isEmpty {
                                Text(payload).font(.caption.monospaced())
                                    .lineLimit(4)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            } else if let error = error {
                Text(error).foregroundStyle(.red)
            } else {
                ProgressView()
            }
        }
        .navigationTitle("Run #\(runId)")
        .toolbar {
            ToolbarItemGroup(placement: .topBarTrailing) {
                if detail?.run.status == .pending
                    || detail?.run.status == .running {
                    Button("Signal") { showSignalSheet = true }
                    Button("Cancel", role: .destructive) {
                        Task { await cancelRun() }
                    }
                }
            }
        }
        .sheet(isPresented: $showSignalSheet) {
            if let detail = detail {
                SignalSheet(runId: detail.run.id)
                    .environmentObject(settings)
            }
        }
        .task {
            await refresh()
            startPolling()
        }
        .onDisappear { pollTask?.cancel() }
    }

    private func refresh() async {
        guard let client = settings.makeClient() else {
            error = "Set the server URL + token in Settings."
            return
        }
        do {
            detail = try await client.runDetail(runId)
            error = nil
        } catch {
            self.error = describe(error)
        }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if Task.isCancelled { return }
                guard let d = detail, !d.run.status.isTerminal else { return }
                await refresh()
            }
        }
    }

    private func cancelRun() async {
        guard let client = settings.makeClient() else { return }
        _ = try? await client.cancel(runId)
        await refresh()
    }
}

@MainActor
private struct SignalSheet: View {
    let runId: Int
    @EnvironmentObject var settings: Settings
    @Environment(\.dismiss) private var dismiss
    @State private var name: String = ""
    @State private var payload: String = ""
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Signal name") {
                    TextField("agent_response", text: $name)
                        .autocorrectionDisabled()
                }
                Section("Payload (optional JSON)") {
                    TextEditor(text: $payload)
                        .frame(minHeight: 100)
                        .font(.body.monospaced())
                }
                if let error = error {
                    Text(error).foregroundStyle(.red)
                }
            }
            .navigationTitle("Send signal")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") { Task { await send() } }
                        .disabled(name.isEmpty)
                }
            }
        }
    }

    private func send() async {
        guard let client = settings.makeClient() else {
            error = "Set the server URL + token in Settings."
            return
        }
        var parsed: [String: Any]? = nil
        if !payload.isEmpty {
            if let data = payload.data(using: .utf8),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                parsed = obj
            } else {
                error = "Payload isn't valid JSON."
                return
            }
        }
        do {
            _ = try await client.signal(runId, name: name, payload: parsed)
            dismiss()
        } catch {
            self.error = describe(error)
        }
    }
}
