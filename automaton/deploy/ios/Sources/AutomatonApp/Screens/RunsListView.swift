// Runs list: pull-to-refresh, color-coded statuses, tap into detail.

import SwiftUI
import AutomatonKit

@MainActor
struct RunsListView: View {
    @EnvironmentObject var settings: Settings
    @State private var runs: [RunSummary] = []
    @State private var error: String?
    @State private var loading = false

    var body: some View {
        NavigationStack {
            List {
                if let error = error {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.red)
                }
                ForEach(runs) { r in
                    NavigationLink(value: r.id) {
                        runRow(r)
                    }
                }
            }
            .refreshable { await refresh() }
            .task { await refresh() }
            .navigationTitle("Runs")
            .navigationDestination(for: Int.self) { runId in
                RunDetailView(runId: runId)
                    .environmentObject(settings)
            }
            .overlay {
                if runs.isEmpty && !loading && error == nil {
                    ContentUnavailableView(
                        "No runs yet",
                        systemImage: "tray",
                        description: Text("Trigger a workflow from the Workflows tab.")
                    )
                }
            }
        }
    }

    private func runRow(_ r: RunSummary) -> some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text("#\(r.id) \(r.workflow)").font(.body)
                if let started = r.startedAt {
                    Text(started).font(.caption2).foregroundStyle(.secondary)
                }
            }
            Spacer()
            statusBadge(r.status)
        }
    }

    private func statusBadge(_ status: RunStatus) -> some View {
        let (label, color): (String, Color) = {
            switch status {
            case .completed:  return ("completed", .green)
            case .failed:     return ("failed",    .red)
            case .running:    return ("running",   .orange)
            case .pending:    return ("pending",   .secondary)
            case .cancelled:  return ("cancelled", .gray)
            }
        }()
        return Text(label)
            .font(.caption.weight(.medium))
            .foregroundStyle(color)
    }

    private func refresh() async {
        loading = true
        defer { loading = false }
        guard let client = settings.makeClient() else {
            error = "Set the server URL + token in Settings."
            return
        }
        do {
            runs = try await client.runs()
            error = nil
        } catch {
            self.error = describe(error)
        }
    }
}

func describe(_ error: Error) -> String {
    if let e = error as? AutomatonError {
        switch e {
        case .invalidURL:              return "Invalid server URL."
        case .unauthorized:             return "Unauthorized - check the bearer token."
        case .notFound(let p):          return "Not found: \(p)"
        case .server(let s, let body):  return "Server \(s): \(body.prefix(120))"
        case .transport(let u):         return "Transport: \(u)"
        case .decoding(let u):          return "Decoding: \(u)"
        }
    }
    return String(describing: error)
}
