// Forensic agents tab (ADR-002 Phase 10a): read-only list of echoes
// agents persisted via the remote-store API, with a chain-linkage badge
// and the latest hash per agent. "Are all my agents green?" at a glance.

import SwiftUI
import AutomatonKit

@MainActor
struct AgentsView: View {
    @EnvironmentObject var settings: Settings
    @State private var agents: [AgentSummary] = []
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                if let error = error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
                if agents.isEmpty && error == nil {
                    Text("No forensic agents registered.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                ForEach(agents) { agent in
                    NavigationLink(value: agent.name) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(agent.name).font(.headline)
                            Text("tick \(agent.tick)"
                                 + (agent.updatedAt.map { " · \($0)" } ?? ""))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationDestination(for: String.self) { name in
                AgentDetailView(name: name).environmentObject(settings)
            }
            .refreshable { await refresh() }
            .task { await refresh() }
            .navigationTitle("Agents")
        }
    }

    private func refresh() async {
        guard let client = settings.makeClient() else {
            error = "Set the server URL + token in Settings."
            return
        }
        do {
            agents = try await client.agents()
            error = nil
        } catch {
            self.error = String(describing: error)
        }
    }
}

@MainActor
struct AgentDetailView: View {
    @EnvironmentObject var settings: Settings
    let name: String
    @State private var entries: [AgentMemoryEntry] = []
    @State private var error: String?
    @State private var loaded = false

    private var linkage: ChainLinkage { ChainLinkage.check(entries) }

    var body: some View {
        List {
            if let error = error {
                Text(error).font(.footnote).foregroundStyle(.red)
            }
            if loaded {
                Section("Integrity") {
                    HStack {
                        Text("Chain linkage")
                        Spacer()
                        linkageBadge
                    }
                    if let last = entries.last {
                        HStack {
                            Text("Latest hash")
                            Spacer()
                            Text(String(last.hash.prefix(16)) + "…")
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                    HStack {
                        Text("Entries")
                        Spacer()
                        Text("\(entries.count)").foregroundStyle(.secondary)
                    }
                }
                Section("Recent memory") {
                    ForEach(entries.suffix(20).reversed()) { e in
                        VStack(alignment: .leading, spacing: 2) {
                            Text("tick \(e.tick)"
                                 + (e.action.map { " · \($0)" } ?? ""))
                                .font(.caption)
                            if let note = e.note, !note.isEmpty {
                                Text(note).font(.caption2)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
        .refreshable { await refresh() }
        .task { await refresh() }
        .navigationTitle(name)
    }

    @ViewBuilder
    private var linkageBadge: some View {
        switch linkage {
        case .linked:
            Label("linked", systemImage: "checkmark.shield.fill")
                .font(.caption).foregroundStyle(.green)
        case .broken:
            Label("BROKEN", systemImage: "exclamationmark.shield.fill")
                .font(.caption.bold()).foregroundStyle(.red)
        case .empty:
            Text("no entries").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func refresh() async {
        guard let client = settings.makeClient() else {
            error = "Set the server URL + token in Settings."
            return
        }
        do {
            entries = try await client.agentEntries(name)
            error = nil
            loaded = true
        } catch {
            self.error = String(describing: error)
        }
    }
}
