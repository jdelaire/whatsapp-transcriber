// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Transcriber",
    platforms: [.macOS(.v15)],
    products: [.library(name: "TranscriberCore", targets: ["TranscriberCore"]), .executable(name: "Transcriber", targets: ["TranscriberApp"])],
    dependencies: [.package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.1")],
    targets: [
        .target(name: "TranscriberCore"),
        .executableTarget(name: "TranscriberApp", dependencies: ["TranscriberCore", .product(name: "Sparkle", package: "Sparkle")], linkerSettings: [.unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"])]),
        .testTarget(name: "TranscriberCoreTests", dependencies: ["TranscriberCore"])
    ]
)
