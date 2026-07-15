// App entry point. Four-tab structure: Runs, Workflows, Agents, Settings.

import SwiftUI

@main
struct AutomatonApp: App {
    @StateObject private var settings = Settings()

    var body: some Scene {
        WindowGroup {
            TabView {
                RunsListView()
                    .tabItem { Label("Runs", systemImage: "list.bullet") }

                WorkflowsView()
                    .tabItem { Label("Workflows", systemImage: "flowchart") }

                AgentsView()
                    .tabItem { Label("Agents", systemImage: "checkmark.shield") }

                SettingsView()
                    .tabItem { Label("Settings", systemImage: "gearshape") }
            }
            .environmentObject(settings)
        }
    }
}
