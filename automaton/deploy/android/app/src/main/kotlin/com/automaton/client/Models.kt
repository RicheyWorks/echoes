// Kotlinx.serialization models matching the JSON API in automaton/ui.py.
//
// Property names use camelCase; the Json decoder is configured with
// namingStrategy = JsonNamingStrategy.SnakeCase so snake_case server
// responses map automatically — the same job Swift's convertFromSnakeCase does.

package com.automaton.client

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

enum class RunStatus {
    pending,
    running,
    completed,
    failed,
    cancelled,
    timed_out;

    val isTerminal: Boolean
        get() = this == completed || this == failed ||
                this == cancelled || this == timed_out
}

@Serializable
data class RunSummary(
    val id: Int,
    val workflow: String,
    val status: String,           // raw string; map to RunStatus.valueOf() defensively
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("finished_at") val finishedAt: String? = null,
    @SerialName("trigger_kind") val triggerKind: String? = null,
) {
    fun runStatus(): RunStatus = runCatching { RunStatus.valueOf(status) }
        .getOrDefault(RunStatus.pending)
}

@Serializable
data class StepDetail(
    val name: String,
    val attempt: Int,
    val status: String,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("finished_at") val finishedAt: String? = null,
    @SerialName("output_json") val outputJson: String? = null,
    @SerialName("error_json") val errorJson: String? = null,
) {
    val stableId: String get() = "$name#$attempt"
}

@Serializable
data class RunEvent(
    val id: Int,
    val ts: String,
    val kind: String,
    @SerialName("payload_json") val payloadJson: String? = null,
)

@Serializable
data class RunRecord(
    val id: Int,
    val workflow: String? = null,
    val version: Int? = null,
    val status: String,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("finished_at") val finishedAt: String? = null,
    @SerialName("trigger_kind") val triggerKind: String? = null,
)

@Serializable
data class RunDetail(
    val run: RunRecord,
    val steps: List<StepDetail>,
    val events: List<RunEvent>,
)

// ------------------------------------------------------------------ //
// forensic agents (ADR-002 Phase 10a)                                  //
// ------------------------------------------------------------------ //

@Serializable
data class AgentSummary(
    val name: String,
    val goal: String? = null,
    val tick: Int = 0,
    @SerialName("updated_at") val updatedAt: String? = null,
)

/** One echoes memory entry from the remote-store API. The `event` field is
 *  deliberately not modeled — it may be a string or a structured object,
 *  and the client only needs the chain fields (ignoreUnknownKeys drops it). */
@Serializable
data class AgentMemoryEntry(
    val tick: Int,
    val action: String? = null,
    val note: String? = null,
    val hash: String,
    @SerialName("prev_hash") val prevHash: String,
)

@Serializable
data class AgentEntriesResponse(
    val entries: List<AgentMemoryEntry>,
    val count: Int? = null,
)

enum class ChainLinkage { LINKED, BROKEN, EMPTY }

/** Client-side linkage check mirroring the server's agents.chain_linkage_ok:
 *  prev-hash linkage only — content-hash verification stays `echoes verify`'s
 *  job. */
fun checkChainLinkage(entries: List<AgentMemoryEntry>): ChainLinkage {
    if (entries.isEmpty()) return ChainLinkage.EMPTY
    var prev = "0".repeat(64)
    for (e in entries) {
        if (e.prevHash != prev) return ChainLinkage.BROKEN
        prev = e.hash
    }
    return ChainLinkage.LINKED
}

@Serializable
data class TriggerResult(
    @SerialName("run_id") val runId: Int,
)

@Serializable
data class SignalResult(
    @SerialName("signal_id") val signalId: Int,
)

@Serializable
data class CancelResult(
    val cancelled: Boolean,
    @SerialName("run_id") val runId: Int? = null,
)
