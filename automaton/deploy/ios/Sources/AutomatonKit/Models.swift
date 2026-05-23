// Codable models matching the JSON API in automaton/ui.py.
//
// Property names use the same snake_case the server emits; Swift's
// JSONDecoder maps them via .convertFromSnakeCase below.

import Foundation

public enum RunStatus: String, Codable, Sendable, CaseIterable {
    case pending
    case running
    case completed
    case failed
    case cancelled

    public var isTerminal: Bool {
        switch self {
        case .completed, .failed, .cancelled: return true
        case .pending, .running:                return false
        }
    }
}

public struct RunSummary: Codable, Identifiable, Sendable {
    public let id: Int
    public let workflow: String
    public let status: RunStatus
    public let startedAt: String?
    public let finishedAt: String?
    public let triggerKind: String?
}

public struct StepDetail: Codable, Identifiable, Sendable {
    // The Python API doesn't expose a numeric id per step row, so we
    // synthesize a stable id from (name, attempt) for SwiftUI lists.
    public var id: String { "\(name)#\(attempt)" }
    public let name: String
    public let attempt: Int
    public let status: String
    public let startedAt: String?
    public let finishedAt: String?
    public let outputJson: String?
    public let errorJson: String?
}

public struct RunEvent: Codable, Identifiable, Sendable {
    public let id: Int
    public let ts: String
    public let kind: String
    public let payloadJson: String?
}

public struct RunRecord: Codable, Sendable {
    public let id: Int
    public let workflow: String?
    public let version: Int?
    public let status: RunStatus
    public let startedAt: String?
    public let finishedAt: String?
    public let triggerKind: String?
}

public struct RunDetail: Codable, Sendable {
    public let run: RunRecord
    public let steps: [StepDetail]
    public let events: [RunEvent]
}

public struct WorkflowDef: Codable, Identifiable, Sendable {
    public var id: Int { workflowDefId ?? 0 }
    public let name: String
    public let workflowDefId: Int?
}

public struct TriggerResult: Codable, Sendable {
    public let runId: Int
}

public struct SignalResult: Codable, Sendable {
    public let signalId: Int
}

public struct CancelResult: Codable, Sendable {
    public let cancelled: Bool
    public let runId: Int?
}
