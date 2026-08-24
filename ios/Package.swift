// swift-tools-version: 6.0
//
// Motet's iOS client, split so that the half worth testing can be tested anywhere.
//
// `MotetKit` is Foundation-only — the API client, the playback state machine, the offline
// library, the outbox, read-state sync — and so it builds and tests on Linux CI with no
// Xcode and no Apple Developer Program membership. `MotetPlayback` is the AVFoundation /
// MediaPlayer half; every file in it is behind `#if canImport(AVFoundation)`, so the
// package still builds where those frameworks do not exist and the app target gets real
// implementations where they do.
//
// The app target itself (SwiftUI screens, the CarPlay scene) lives in `App/`, driven by
// `App/Motet.xcodeproj`, because an iOS application bundle is not something SwiftPM builds.

import PackageDescription

let package = Package(
    name: "MotetKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "MotetKit", targets: ["MotetKit"]),
        .library(name: "MotetPlayback", targets: ["MotetPlayback"]),
    ],
    targets: [
        .target(name: "MotetKit"),
        .target(name: "MotetPlayback", dependencies: ["MotetKit"]),
        .testTarget(name: "MotetKitTests", dependencies: ["MotetKit"]),
    ]
)
