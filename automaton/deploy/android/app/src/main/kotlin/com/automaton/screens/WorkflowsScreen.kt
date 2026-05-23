// Workflows list: synthesised from recent runs; one-tap trigger.

package com.automaton.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.automaton.Settings
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WorkflowsScreen(settings: Settings) {
    var workflows by remember { mutableStateOf<List<String>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var triggerResult by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(Unit) {
        val client = settings.makeClient()
        if (client == null) {
            error = "Set the server URL + token in Settings."
        } else {
            runCatching { client.workflows() }
                .onSuccess { workflows = it; error = null }
                .onFailure { error = it.message }
        }
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Workflows") }) }
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            error?.let {
                Text(it, color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(12.dp))
            }
            triggerResult?.let {
                Text(it, style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.padding(horizontal = 12.dp))
            }
            LazyColumn {
                items(workflows) { wf ->
                    Row(
                        modifier = Modifier.fillMaxWidth()
                            .padding(horizontal = 16.dp, vertical = 10.dp),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Text(wf, style = MaterialTheme.typography.bodyLarge,
                            modifier = Modifier.weight(1f))
                        Button(onClick = {
                            scope.launch {
                                val client = settings.makeClient() ?: return@launch
                                runCatching { client.trigger(wf) }
                                    .onSuccess { triggerResult = "Triggered run #$it" }
                                    .onFailure { triggerResult = "Error: ${it.message}" }
                            }
                        }) { Text("Trigger") }
                    }
                    HorizontalDivider()
                }
            }
        }
    }
}
