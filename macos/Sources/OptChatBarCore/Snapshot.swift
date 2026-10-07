import Foundation

public struct Operation: Codable, Sendable {
    public let count: Int
    public let errors: Int
    public let total_ms: Double
    public let max_ms: Double
    public let recent_ms: [Double]?
    public var average: Double { count == 0 ? 0 : total_ms / Double(count) }
    public var recentMedian: Double? {
        guard let recent_ms, !recent_ms.isEmpty else { return nil }
        let sorted = recent_ms.sorted()
        let middle = sorted.count / 2
        if sorted.count.isMultiple(of: 2) {
            return (sorted[middle - 1] + sorted[middle]) / 2
        }
        return sorted[middle]
    }
}

public struct Metrics: Codable, Sendable {
    public let since: Double
    public let operations: [String: Operation]
    public let hourly: [String: [String: Int]]
    public let tokens: [String: Double]
    public let usage_calls: Int
    public let unreported_calls: Int
    public let active_summarizers: Int?
}

public struct DaemonStatus: Codable, Sendable {
    public let messages: Int
    public let nodes: Int
    public let view_parts: Int
    public let view_bytes: Int
    public let view_budget: Int
    public let settled: Bool
    public let busy: [String]
    public let failures: [String: String]
    public let pending_leaves: Int?
    public let queued_nodes: Int?
    public let worker_limit: Int?
    public let pid: Int?
}

public enum Health: String, Sendable {
    case offline = "Daemon offline"
    case failed = "Retrying failed work"
    case working = "Processing"
    case settled = "Settled"
    case waiting = "Waiting for summaries"
}

public struct Snapshot: Codable, Sendable {
    public let home: String
    public let timestamp: Double
    public let running: Bool
    public let status: DaemonStatus?
    public let metrics: Metrics?
    public let error: String?
    public let disk_bytes: Int64

    public var health: Health {
        guard running, let status else { return .offline }
        if !status.failures.isEmpty { return .failed }
        // View settling can finish while higher tree nodes still compact.
        if !status.busy.isEmpty || (metrics?.active_summarizers ?? 0) > 0 { return .working }
        return status.settled ? .settled : .waiting
    }

    public func hourlyCounts(now: Date = Date()) -> [(Date, Int)] {
        let hour = Int(now.timeIntervalSince1970 / 3600) * 3600
        return (0..<24).map { offset in
            let start = hour - (23 - offset) * 3600
            return (Date(timeIntervalSince1970: Double(start)), metrics?.hourly[String(start)]?["nodes"] ?? 0)
        }
    }
}

public enum SnapshotLoader {
    public static func load(python: String, resources: String, home: String) throws -> Snapshot {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-m", "optchat", "--home", home, "monitor"]
        process.environment = ProcessInfo.processInfo.environment.merging([
            "PYTHONPATH": resources, "OPTCHAT_INTERNAL": "1", "PYTHONDONTWRITEBYTECODE": "1"
        ]) { _, new in new }
        let output = Pipe()
        let errors = Pipe()
        process.standardOutput = output
        process.standardError = errors
        try process.run()
        // Drain before waiting, so a larger failure list cannot fill the pipe.
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else {
            let message = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? "Probe failed"
            throw NSError(domain: "OptChatBar", code: Int(process.terminationStatus),
                          userInfo: [NSLocalizedDescriptionKey: message])
        }
        return try JSONDecoder().decode(Snapshot.self, from: data)
    }
}
