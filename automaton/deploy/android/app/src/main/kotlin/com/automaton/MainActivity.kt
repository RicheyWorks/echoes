// Entry point. A bottom NavigationBar with three tabs mirrors the iOS
// TabView: Runs, Workflows, Settings.

package com.automaton

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.navigation.NavType
import androidx.navigation.compose.*
import androidx.navigation.navArgument
import com.automaton.screens.*

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val settings = Settings(applicationContext)
        setContent {
            AutomatonTheme {
                AutomatonApp(settings)
            }
        }
    }
}

@Composable
private fun AutomatonTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = dynamicColorScheme(),
        content = content,
    )
}

@Composable
private fun dynamicColorScheme() = MaterialTheme.colorScheme  // system default

private sealed class Tab(val route: String, val label: String) {
    object Runs      : Tab("runs",      "Runs")
    object Workflows : Tab("workflows", "Workflows")
    object Settings  : Tab("settings",  "Settings")
}

@Composable
private fun AutomatonApp(settings: Settings) {
    val navController = rememberNavController()
    val tabs = listOf(Tab.Runs, Tab.Workflows, Tab.Settings)
    val backStack by navController.currentBackStackEntryAsState()
    val currentRoute = backStack?.destination?.route

    Scaffold(
        bottomBar = {
            NavigationBar {
                tabs.forEach { tab ->
                    NavigationBarItem(
                        selected = currentRoute?.startsWith(tab.route) == true,
                        onClick = {
                            navController.navigate(tab.route) {
                                popUpTo(navController.graph.startDestinationId) {
                                    saveState = true
                                }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = {
                            when (tab) {
                                Tab.Runs      -> Icon(Icons.Filled.List,      tab.label)
                                Tab.Workflows -> Icon(Icons.Filled.PlayArrow, tab.label)
                                Tab.Settings  -> Icon(Icons.Filled.Settings,  tab.label)
                            }
                        },
                        label = { Text(tab.label) },
                    )
                }
            }
        }
    ) { innerPadding ->
        NavHost(navController, startDestination = Tab.Runs.route,
                modifier = Modifier.padding(innerPadding)) {
            composable(Tab.Runs.route) {
                RunsListScreen(settings) { runId ->
                    navController.navigate("run/$runId")
                }
            }
            composable(
                route = "run/{runId}",
                arguments = listOf(navArgument("runId") { type = NavType.IntType })
            ) { back ->
                val runId = back.arguments!!.getInt("runId")
                RunDetailScreen(runId, settings) { navController.popBackStack() }
            }
            composable(Tab.Workflows.route) {
                WorkflowsScreen(settings)
            }
            composable(Tab.Settings.route) {
                SettingsScreen(settings)
            }
        }
    }
}
