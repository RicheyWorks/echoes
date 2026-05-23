// Run detail: step tree, event log, output. Polls while pending/running.

package com.automaton.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.automaton.Settings
import com.automaton.client.RunDetail
import com.automaton.client.RunStatus
import com.automaton.client.StepDetail
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private const val POLL_INTERVAL_MS = 2_000L

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RunDetailScreen(
    runId: Int,
    settings: Settings,
    onBack: () -> Unit,
) {
    var detail by remember { mutableStateOf<RunDetail?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    // Poll while run is active; stop once terminal.
    LaunchedEffect(runId) {
        while (true) {
            val client = settings.makeClient() ?: break
            runCatching { client.runDetail(runId) }
                .onSuccess { detail = it; error = null }
                .onFailure { error = it.message }
            val status = detail?.run?.status?.let {
                runCatching { RunStatus.valueOf(it) }.getOrNull()
            }
            if (status != null && status.isTerminal) break
            delay(POLL_INTERVAL_MS)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Run #$runId") },
                navigationIcon = {
                    TextButton(onClick = onBack) { Text("Back") }
                },
            )
        }
    ) { padding ->
        val d = detail
        if (d == null) {
            Box(Modifier.fillMaxSize().padding(padding)) {
                if (error != null) Text(error ?: "",
                    color = MaterialTheme.colorScheme.error)
                else CircularProgressIndicator()
            }
            return@Scaffold
        }

        LazyColumn(contentPadding = padding,
                   modifier = Modifier.fillMaxSize()) {
            // Run summary header
            item {
                Card(Modifier.fillMaxWidth().padding(12.dp)) {
                    Column(Modifier.padding(12.dp)) {
                        Text("${d.run.workflow ?: "?"} v${d.run.version ?: "?"}",
                            style = MaterialTheme.typography.titleMedium)
                        val status = runCatching {
                            RunStatus.valueOf(d.run.status)
                        }.getOrNull() ?: RunStatus.pending
                        StatusBadge(status)
                        d.run.startedAt?.let { Text("Started: $it",
                            style = MaterialTheme.typography.labelSmall) }
                        d.run.finishedAt?.let { Text("Finished: $it",
                            style = MaterialTheme.typography.labelSmall) }
                    }
                }
            }

            // Steps
            item {
                Text("Steps", style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.padding(start = 12.dp, top = 8.dp))
            }
            items(d.steps, key = { it.stableId }) { step ->
                StepCard(step)
            }

            // Signal respond button (if run is parked at wait_for_signal)
            if (d.run.status == "running" &&
                d.steps.any { it.name.startsWith("wait") || it.status == "running" }) {
                item { SignalResponder(runId, settings) }
            }

            // Event log
            item {
                Text("Event log", style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.padding(start = 12.dp, top = 12.dp))
            }
            items(d.events, key = { it.id }) { evt ->
                Row(Modifier.padding(horizontal = 12.dp, vertical = 2.dp)) {
                    Text(evt.ts, style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.width(160.dp))
                    Text(evt.kind, style = MaterialTheme.typography.labelSmall)
                }
            }
        }
    }
}

@Composable
private fun StepCard(step: StepDetail) {
    Card(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp)) {
        Column(Modifier.padding(10.dp)) {
            Row(horizontalArrangement = Arrangement.SpaceBetween,
                modifier = Modifier.fillMaxWidth()) {
                Text("${step.name} (attempt ${step.attempt})",
                    style = MaterialTheme.typography.bodyMedium)
                Text(step.status, style = MaterialTheme.typography.labelSmall)
            }
            step.outputJson?.let {
                Text(it, style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(top = 4.dp))
            }
            step.errorJson?.let {
                Text(it, style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 4.dp))
            }
        }
    }
}

@Composable
private fun SignalResponder(runId: Int, settings: Settings) {
    var signalName by remember { mutableStateOf("") }
    var payload by remember { mutableStateOf("") }
    var sent by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    Card(Modifier.fillMaxWidth().padding(12.dp)) {
        Column(Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Respond to signal", style = MaterialTheme.typography.titleSmall)
            OutlinedTextField(value = signalName,
                onValueChange = { signalName = it },
                label = { Text("Signal name") },
                modifier = Modifier.fillMaxWidth())
            OutlinedTextField(value = payload,
                onValueChange = { payload = it },
                label = { Text("Payload (optional JSON)") },
                modifier = Modifier.fillMaxWidth())
            Button(onClick = {
                scope.launch {
                    val client = settings.makeClient() ?: return@launch
                    runCatching {
                        val p = if (payload.isBlank()) null
                                else mapOf("data" to payload)
                        client.signal(runId, signalName, p)
                    }
                        .onSuccess { sent = "Signal sent (id $it)" }
                        .onFailure { sent = "Error: ${it.message}" }
                }
            }) { Text("Send") }
            sent?.let { Text(it, style = MaterialTheme.typography.labelSmall) }
        }
    }
}
