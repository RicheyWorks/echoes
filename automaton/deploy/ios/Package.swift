// swift-tools-version:5.9
//
// Swift package for the automaton iOS client.
//
// Build for the simulator:
//   swift build
// Open in Xcode to deploy to a device or to TestFlight:
//   open Package.swift  (Xcode picks it up as a Swift Package)
//
// AutomatonKit is the pure API client + Codable models. It's a separate
// product so the same code can drive a future macOS menubar app or an
// Apple Watch surface without dragging the SwiftUI views along.

import PackageDescription

let package = Package(
    name: "automaton",
    platforms: [
        .iOS(.v17),
        .macOS(.v14),
    ],
    products: [
        .library(name: "AutomatonKit", targets: ["AutomatonKit"]),
        .executable(name: "AutomatonApp", targets: ["AutomatonApp"]),
    ],
    dependencies: [],
    targets: [
        .target(
            name: "AutomatonKit",
            path: "Sources/AutomatonKit"
        ),
        .executableTarget(
            name: "AutomatonApp",
            dependencies: ["AutomatonKit"],
            path: "Sources/AutomatonApp"
        ),
    ]
)
