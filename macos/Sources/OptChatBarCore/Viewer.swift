import Foundation

public struct MemoryLine: Decodable, Sendable, Identifiable {
    public let start: Int
    public let n: Int
    public let built: Bool
    public let text: String

    public var id: String { "\(start)+\(n)" }
}

public struct MemoryViewPayload: Decodable, Sendable {
    public let messages: Int
    public let settled: Bool
    public let view_bytes: Int
    public let view_budget: Int
    public let lines: [MemoryLine]
}

public enum ViewerProbe {
    public static let placeholder = "(not summarized yet: zoom it)"

    public static func data(python: String, resources: String, home: String, arguments: [String]) throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "optchat", "--home", home] + arguments
        process.environment = ProcessInfo.processInfo.environment.merging([
            "PYTHONPATH": resources, "OPTCHAT_INTERNAL": "1",
            "OPTCHAT_NO_AUTOSTART": "1", "PYTHONDONTWRITEBYTECODE": "1"
        ]) { _, new in new }
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        try process.run()
        // Drain before waiting, so a long failure list cannot fill the pipe.
        let result = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            let raw = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            let message = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            throw NSError(domain: "OptChatBar", code: Int(process.terminationStatus),
                          userInfo: [NSLocalizedDescriptionKey: message.isEmpty ? "The daemon is not running." : message])
        }
        return result
    }

    public static func view(python: String, resources: String, home: String) throws -> MemoryViewPayload {
        try JSONDecoder().decode(MemoryViewPayload.self,
                                 from: data(python: python, resources: resources, home: home, arguments: ["view"]))
    }

    public static func zoom(python: String, resources: String, home: String, start: Int, n: Int) throws -> [MemoryLine] {
        let raw = try data(python: python, resources: resources, home: home,
                           arguments: ["zoom", String(start), String(n)])
        return parseZoom(String(decoding: raw, as: UTF8.self))
    }

    public static func parseZoom(_ text: String) -> [MemoryLine] {
        text.split(separator: "\n", omittingEmptySubsequences: true).compactMap { raw in
            guard let pipe = raw.firstIndex(of: "|") else { return nil }
            let head = raw[..<pipe].split(separator: "+")
            guard head.count == 2, let start = Int(head[0]), let n = Int(head[1]) else { return nil }
            let value = String(raw[raw.index(after: pipe)...])
            return MemoryLine(start: start, n: n, built: value != placeholder, text: value)
        }
    }
}
