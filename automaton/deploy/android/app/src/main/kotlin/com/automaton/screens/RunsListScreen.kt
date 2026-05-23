// Runs list: pull-to-refresh, color-coded statuses, tap into detail.

package com.automaton.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.automaton.Settings
import com.automaton.client.RunStatus
import com.automaton.client.RunSummary
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RunsListScreen(
    settings: Settings,
    onRunSelected: (Int) -> Unit,
) {
    var runs by remember { mutableStateOf<List<RunSummary>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var refreshing by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    fun refresh() {
        scope.launch {
            refreshing = true
            val client = settings.makeClient()
            if (client == null) {
                error = "Set the server URL + token in Settings."
            } else {
                runCatching { client.runs() }
                    .onSuccess { runs = it; error = null }
                    .onFailure { error = it.message }
            }
            refreshing = false
        }
    }

    LaunchedEffect(Unit) { refresh() }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Runs") }) }
    ) { padding ->
        if (error != null) {
            Box(Modifier.fillMaxSize().padding(padding),
                contentAlignment = Alignment.Center) {
                Text(error ?: "", color = MaterialTheme.colorScheme.error)
            }
            return@Scaffold
        }
        LazyColumn(contentPadding = padding) {
            items(runs, key = { it.id }) { run ->
                RunRow(run = run, onClick = { onRunSelected(run.id) })
                HorizontalDivider()
            }
            if (runs.isEmpty() && !refreshing) {
                item {
                    Box(Modifier.fillParentMaxSize(),
                        contentAlignment = Alignment.Center) {
                        Text("No runs yet.", color = MaterialTheme.colorScheme.secondary)
                    }
                }
            }
        }
    }
}

@Composable
private fun RunRow(run: RunSummary, onClick: () -> Unit) {
    val status = run.runStatus()
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text("#${run.id} ${run.workflow}",
                style = MaterialTheme.typography.bodyLarge)
            run.startedAt?.let {
                Text(it, style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.secondary)
            }
        }
        StatusBadge(status)
    }
}

@Composable
fun StatusBadge(status: RunStatus) {
    val color = when (status) {
        RunStatus.completed -> Color(0xFF2E7D32)   // green
        RunStatus.failed    -> Color(0xFFC62828)   // red
        RunStatus.running   -> Color(0xFFE65100)   // orange
        RunStatus.timed_out -> Color(0xFF6A1B9A)   // purple
        RunStatus.pending   -> Color(0xFF757575)   // grey
        RunStatus.cancelled -> Color(0xFF9E9E9E)   // light grey
    }
    Text(
        text = status.name,
        style = MaterialTheme.typography.labelMedium,
        color = color,
    )
}
