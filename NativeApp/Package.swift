// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Transcriber",
    platforms: [.macOS(.v15)],
    products: [.library(name: "TranscriberCore", targets: ["TranscriberCore"]), .executable(name: "Transcriber", targets: ["TranscriberApp"])],
    targets: [
        .target(name: "TranscriberCore"),
        .executableTarget(name: "TranscriberApp", dependencies: ["TranscriberCore"]),
        .testTarget(name: "TranscriberCoreTests", dependencies: ["TranscriberCore"])
    ]
)
