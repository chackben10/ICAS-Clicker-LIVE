// swift-tools-version: 5.10

import PackageDescription

let package = Package(
    name: "ProductionHubVideoToNDI",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .library(name: "VideoToNDICore", targets: ["VideoToNDICore"]),
        .executable(name: "VideoToNDIApp", targets: ["VideoToNDIApp"]),
        .executable(name: "ndi-probe", targets: ["NDIProbe"]),
    ],
    targets: [
        .target(
            name: "CNDIShim",
            path: "Sources/CNDIShim",
            publicHeadersPath: "include",
            linkerSettings: [
                .linkedLibrary("dl")
            ]
        ),
        .target(
            name: "VideoToNDICore",
            dependencies: ["CNDIShim"],
            path: "Sources/VideoToNDICore"
        ),
        .executableTarget(
            name: "VideoToNDIApp",
            dependencies: ["VideoToNDICore"],
            path: "Sources/VideoToNDIApp"
        ),
        .executableTarget(
            name: "NDIProbe",
            dependencies: ["VideoToNDICore"],
            path: "Sources/NDIProbe"
        ),
        .testTarget(
            name: "VideoToNDITests",
            dependencies: ["VideoToNDICore"],
            path: "Tests/VideoToNDITests"
        ),
    ]
)
