import XCTest
@testable import OptChatBarCore

final class SnapshotTests: XCTestCase {
    func decode(running: Bool = true, settled: Bool = true, busy: String = "[]", failures: String = "{}", metrics: String = "null") throws -> Snapshot {
        let json = """
        {"home":"/tmp/memory","timestamp":100,"running":\(running),"disk_bytes":4096,
        "status":{"messages":30,"nodes":56,"view_parts":5,"view_bytes":1000,"view_budget":128000,
        "settled":\(settled),"busy":\(busy),"failures":\(failures)},"metrics":\(metrics),"error":null}
        """
        return try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
    }

    func testHealthPrecedenceAndOldDaemonCompatibility() throws {
        XCTAssertEqual(try decode().health, .settled)
        XCTAssertEqual(try decode(running: false).health, .offline)
        XCTAssertEqual(try decode(settled: false).health, .waiting)
        XCTAssertEqual(try decode(busy: "[\"0+8\"]").health, .working)
        XCTAssertEqual(try decode(busy: "[\"0+8\"]", failures: "{\"0+1\":\"provider unavailable\"}").health, .failed)
        XCTAssertNil(try decode().status?.pending_leaves)
    }

    func testChartPadsEmptyHoursAndIgnoresOldBuckets() throws {
        let metrics = """
        {"since":0,"operations":{},"tokens":{},"usage_calls":0,"unreported_calls":0,
        "hourly":{"3600":{"nodes":9},"86400":{"nodes":3},"90000":{"nodes":5}},"active_summarizers":2}
        """
        let snapshot = try decode(metrics: metrics)
        let points = snapshot.hourlyCounts(now: Date(timeIntervalSince1970: 90001))
        XCTAssertEqual(points.count, 24)
        XCTAssertEqual(points.last?.1, 5)
        XCTAssertEqual(points.reduce(0) { $0 + $1.1 }, 8)
        XCTAssertEqual(snapshot.health, .working)
    }

    func testPopoverFitsShortDisplayWithoutClippingHeader() {
        let visibleHeight = 604.0
        XCTAssertLessThanOrEqual(PopoverLayout.height(availableHeight: visibleHeight), visibleHeight - 24,
                                 "Popover must fit below the menu bar so the title remains on screen")
    }

    func testOfflineSnapshotWithoutStatus() throws {
        let json = """
        {"home":"/tmp/memory","timestamp":100,"running":false,"disk_bytes":0,"status":null,"metrics":null,"error":"not running"}
        """
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: Data(json.utf8))
        XCTAssertEqual(snapshot.health, .offline)
        XCTAssertEqual(snapshot.hourlyCounts().count, 24)
    }
}
