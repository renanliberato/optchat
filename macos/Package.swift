// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "OptChatBar",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "OptChatBar", targets: ["OptChatBar"])],
    targets: [
        .target(name: "OptChatBarCore"),
        .executableTarget(name: "OptChatBar", dependencies: ["OptChatBarCore"]),
        .testTarget(name: "OptChatBarCoreTests", dependencies: ["OptChatBarCore"])
    ]
)
