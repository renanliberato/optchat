import XCTest
import OptChatBarCore

final class ViewerTests: XCTestCase {
    func testZoomLinesParse() throws {
        let lines = ViewerProbe.parseZoom("12+4|user: a; talk: b\n16+4|tool: c")
        XCTAssertEqual(lines.count, 2)
        XCTAssertEqual(lines[0].start, 12)
        XCTAssertEqual(lines[0].n, 4)
        XCTAssertTrue(lines[0].built)
        XCTAssertEqual(lines[0].text, "user: a; talk: b")
        XCTAssertEqual(lines[0].id, "12+4")
    }

    func testPlaceholderCountsAsUnbuilt() throws {
        let lines = ViewerProbe.parseZoom("0+1|user: hello\n2+2|\(ViewerProbe.placeholder)")
        XCTAssertEqual(lines.count, 2)
        XCTAssertTrue(lines[0].built)
        XCTAssertFalse(lines[1].built)
    }

    func testMalformedLinesAreSkipped() throws {
        let lines = ViewerProbe.parseZoom("garbage\n1+2|ok\n+")
        XCTAssertEqual(lines.count, 1)
        XCTAssertEqual(lines[0].text, "ok")
    }
}
