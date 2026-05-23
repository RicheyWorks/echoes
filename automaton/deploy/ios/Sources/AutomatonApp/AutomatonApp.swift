// App entry point. Three-tab structure: Runs, Workflows, Settings.

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

                SettingsView()
                    .tabItem { Label("Settings", systemImage: "gearshape") }
            }
            .environmentObject(settings)
        }
    }
}
