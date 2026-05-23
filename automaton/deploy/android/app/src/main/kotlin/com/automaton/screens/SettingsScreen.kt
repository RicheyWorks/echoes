// Settings: server URL (plain prefs), bearer token (EncryptedSharedPreferences),
// optional cert fingerprint for self-signed certs, connection health-check.

package com.automaton.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.automaton.Settings
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(settings: Settings) {
    var url by remember { mutableStateOf(settings.serverUrl) }
    var token by remember { mutableStateOf(settings.token) }
    var fingerprint by remember { mutableStateOf(settings.pinnedFingerprint) }
    var healthResult by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()

    fun save() {
        settings.serverUrl = url.trim()
        settings.token = token.trim()
        settings.pinnedFingerprint = fingerprint.trim()
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Settings") }) }
    ) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            OutlinedTextField(
                value = url,
                onValueChange = { url = it },
                label = { Text("Server URL") },
                placeholder = { Text("https://your-host:8080") },
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = token,
                onValueChange = { token = it },
                label = { Text("Bearer token") },
                visualTransformation = PasswordVisualTransformation(),
                modifier = Modifier.fillMaxWidth(),
            )
            OutlinedTextField(
                value = fingerprint,
                onValueChange = { fingerprint = it },
                label = { Text("Cert fingerprint (SHA-256, optional)") },
                placeholder = { Text("ab:cd:ef:… for self-signed certs") },
                modifier = Modifier.fillMaxWidth(),
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = {
                    save()
                    scope.launch {
                        val client = settings.makeClient()
                        if (client == null) {
                            healthResult = "Server URL is empty."
                            return@launch
                        }
                        runCatching { client.health() }
                            .onSuccess { ok ->
                                healthResult = if (ok) "Connected ✓" else "Health check failed"
                            }
                            .onFailure { healthResult = "Error: ${it.message}" }
                    }
                }) { Text("Save & Test") }
                OutlinedButton(onClick = ::save) { Text("Save") }
            }
            healthResult?.let {
                Text(it, style = MaterialTheme.typography.labelMedium)
            }
            Spacer(Modifier.weight(1f))
            Text(
                "Sideload: build with ./gradlew assembleDebug, copy the APK to " +
                "your device, and install via the Files app (allow unknown sources " +
                "in Settings → Security).",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.secondary,
            )
        }
    }
}
