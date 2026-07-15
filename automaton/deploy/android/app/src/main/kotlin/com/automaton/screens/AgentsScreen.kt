// Forensic agents tab (ADR-002 Phase 10a): read-only list of echoes agents
// persisted via the remote-store API, with a chain-linkage badge and the
// latest hash per agent. "Are all my agents green?" at a glance.

package com.automaton.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.automaton.Settings
import com.automaton.client.AgentMemoryEntry
import com.automaton.client.AgentSummary
import com.automaton.client.ChainLinkage
import com.automaton.client.checkChainLinkage

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AgentsScreen(settings: Settings) {
    var agents by remember { mutableStateOf<List<AgentSummary>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var selected by remember { mutableStateOf<AgentSummary?>(null) }

    LaunchedEffect(Unit) {
        val client = settings.makeClient()
        if (client == null) {
            error = "Set the server URL + token in Settings."
        } else {
            runCatching { client.agents() }
                .onSuccess { agents = it; error = null }
                .onFailure { error = it.message }
        }
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Agents") }) }
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            error?.let {
                Text(it, color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(12.dp))
            }
            if (agents.isEmpty() && error == null) {
                Text("No forensic agents registered.",
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(12.dp))
            }
            LazyColumn {
                items(agents, key = { it.name }) { agent ->
                    ListItem(
                        headlineContent = { Text(agent.name) },
                        supportingContent = {
                            Text("tick ${agent.tick}" +
                                (agent.updatedAt?.let { " · $it" } ?: ""))
                        },
                        modifier = Modifier.padding(horizontal = 4.dp),
                        trailingContent = {
                            TextButton(onClick = { selected = agent }) {
                                Text("Inspect")
                            }
                        },
                    )
                    HorizontalDivider()
                }
            }
        }
        selected?.let { agent ->
            AgentDetailDialog(agent, settings) { selected = null }
        }
    }
}

@Composable
private fun AgentDetailDialog(
    agent: AgentSummary,
    settings: Settings,
    onDismiss: () -> Unit,
) {
    var entries by remember { mutableStateOf<List<AgentMemoryEntry>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loaded by remember { mutableStateOf(false) }

    LaunchedEffect(agent.name) {
        val client = settings.makeClient()
        if (client == null) {
            error = "Set the server URL + token in Settings."
        } else {
            runCatching { client.agentEntries(agent.name) }
                .onSuccess { entries = it; error = null; loaded = true }
                .onFailure { error = it.message }
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("Close") }
        },
        title = { Text(agent.name) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                error?.let {
                    Text(it, color = MaterialTheme.colorScheme.error)
                }
                if (loaded) {
                    val linkage = checkChainLinkage(entries)
                    Row(verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text("Chain linkage:")
                        when (linkage) {
                            ChainLinkage.LINKED -> Text(
                                "linked",
                                color = MaterialTheme.colorScheme.primary)
                            ChainLinkage.BROKEN -> Text(
                                "BROKEN",
                                color = MaterialTheme.colorScheme.error,
                                fontWeight = FontWeight.Bold)
                            ChainLinkage.EMPTY -> Text("no entries")
                        }
                    }
                    entries.lastOrNull()?.let { last ->
                        Text(
                            "latest hash ${last.hash.take(16)}…",
                            style = MaterialTheme.typography.bodySmall,
                            fontFamily = FontFamily.Monospace,
                        )
                    }
                    Text("${entries.size} entries",
                        style = MaterialTheme.typography.bodySmall)
                    agent.goal?.let {
                        Text("goal: $it",
                            style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        },
    )
}
