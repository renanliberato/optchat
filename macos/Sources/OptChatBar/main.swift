import AppKit
import SwiftUI
import Charts
import OptChatBarCore

@MainActor
final class Monitor: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var error: String?
    @Published var refreshing = false
    @Published var home: String
    private var timer: Timer?
    let python: String
    let resources: String

    init() {
        home = ProcessInfo.processInfo.environment["OPTCHAT_HOME"]
            ?? UserDefaults.standard.string(forKey: "OptChatHome")
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".optchat").path
        resources = Bundle.main.resourceURL!.appendingPathComponent("python").path
        let configURL = Bundle.main.resourceURL!.appendingPathComponent("runtime.json")
        let config = (try? Data(contentsOf: configURL)).flatMap {
            try? JSONSerialization.jsonObject(with: $0) as? [String: String]
        }
        python = config?["python"] ?? "/usr/bin/python3"
    }

    func start() {
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        let python = python, resources = resources, home = home
        Task {
            let result = await Task.detached(priority: .utility) {
                Result { try SnapshotLoader.load(python: python, resources: resources, home: home) }
            }.value
            refreshing = false
            // A folder selection during the probe invalidates its result.
            guard self.home == home else { refresh(); return }
            switch result {
            case .success(let value): snapshot = value; error = nil
            case .failure(let failure): error = failure.localizedDescription; snapshot = nil
            }
            NotificationCenter.default.post(name: .init("OptChatUpdated"), object: nil)
        }
    }

    func chooseHome() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = false
        panel.showsHiddenFiles = true
        panel.message = "Choose the OptChat memory folder containing config.json, main, and tree."
        panel.directoryURL = URL(fileURLWithPath: home)
        NSApp.activate(ignoringOtherApps: true)
        if panel.runModal() == .OK, let url = panel.url {
            home = url.path
            UserDefaults.standard.set(home, forKey: "OptChatHome")
            snapshot = nil
            refresh()
        }
    }
}

func compact(_ value: Int) -> String {
    value.formatted(.number.notation(.compactName))
}

func bytes(_ value: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: value, countStyle: .file)
}

func latency(_ milliseconds: Double) -> String {
    milliseconds < 1000 ? String(format: "%.0f ms", milliseconds) : String(format: "%.2f s", milliseconds / 1000)
}

extension Health {
    var color: Color {
        switch self {
        case .settled: .green
        case .working: .blue
        case .waiting: .orange
        case .failed: .red
        case .offline: .secondary
        }
    }
}

struct Dashboard: View {
    @ObservedObject var monitor: Monitor
    var height: CGFloat = 720
    var openViewer: () -> Void = {}
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "bubble.left.and.text.bubble.right.fill")
                    .font(.title2).foregroundStyle(.teal)
                VStack(alignment: .leading, spacing: 2) {
                    Text("OptChat").font(.title3.bold())
                    Text("LOCAL MEMORY").font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                }
                Spacer()
                if monitor.refreshing { ProgressView().controlSize(.small) }
                Button { monitor.refresh() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain).help("Refresh statistics")
            }.padding(18)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let snapshot = monitor.snapshot {
                        content(snapshot)
                    } else {
                        Label(monitor.error == nil ? "Connecting to local memory…" : "Unable to read statistics", systemImage: "exclamationmark.circle")
                        if let error = monitor.error { Text(error).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
                    }
                    HStack {
                        Image(systemName: "folder").foregroundStyle(.secondary)
                        Text(monitor.home).font(.caption).lineLimit(1).truncationMode(.middle).help(monitor.home)
                        Spacer()
                        Button("Change…") { monitor.chooseHome() }.buttonStyle(.link)
                    }
                }.padding(18)
            }
            Divider()
            HStack {
                Button("Memory Viewer…") { openViewer() }
                Spacer()
                Button("Open memory folder") { NSWorkspace.shared.open(URL(fileURLWithPath: monitor.home)) }
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.keyboardShortcut("q")
            }.buttonStyle(.plain).font(.caption).padding(14)
        }.frame(width: 420, height: height)
            .background(Color(nsColor: .windowBackgroundColor))
    }

    @ViewBuilder func content(_ snapshot: Snapshot) -> some View {
        HStack {
            Circle().fill(snapshot.health.color).frame(width: 9, height: 9)
            Text(snapshot.health.rawValue).font(.headline)
            Spacer()
            if let pid = snapshot.status?.pid { Text("PID \(pid)").font(.caption.monospacedDigit()).foregroundStyle(.secondary) }
        }
        if let status = snapshot.status {
            HStack(spacing: 8) {
                tile("Messages", compact(status.messages), "text.bubble")
                tile("Summary nodes", compact(status.nodes), "square.stack.3d.up")
                tile("Disk", bytes(snapshot.disk_bytes), "internaldrive")
            }
            HStack(spacing: 8) {
                tile("Pending messages", status.pending_leaves.map(compact) ?? "—", "tray")
                tile("Queued nodes", status.queued_nodes.map(compact) ?? "—", "line.3.horizontal.decrease")
                tile("Summarizers", snapshot.metrics?.active_summarizers.map { "\($0) / \(status.worker_limit ?? 8)" } ?? "—", "bolt")
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text("Memory view").font(.caption.bold())
                    Spacer()
                    Text("\(bytes(Int64(status.view_bytes))) / \(bytes(Int64(status.view_budget)))").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                ProgressView(value: min(1, Double(status.view_bytes) / Double(max(1, status.view_budget))))
                    .tint(status.view_bytes > status.view_budget ? .orange : .teal)
                Text(status.settled ? "View ready · \(status.view_parts) entries" : "Waiting for the view’s message summaries")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if !status.failures.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Label("\(status.failures.count) failed nodes · daemon will retry", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red)
                    ForEach(status.failures.keys.sorted().prefix(3), id: \.self) { key in
                        Text("\(key): \(status.failures[key] ?? "")").lineLimit(3).textSelection(.enabled)
                    }
                }.font(.caption)
            }
        } else {
            Text("The daemon is not responding. Monitoring does not start it.").font(.caption).foregroundStyle(.secondary)
            HStack { Text("Local storage"); Spacer(); Text(bytes(snapshot.disk_bytes)).monospacedDigit() }.font(.caption)
        }
        if let metrics = snapshot.metrics {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("Nodes processed").font(.headline)
                    Spacer()
                    Text("Last 24 hours").font(.caption).foregroundStyle(.secondary)
                }
                Chart(snapshot.hourlyCounts(), id: \.0) { date, count in
                    BarMark(x: .value("Hour", date, unit: .hour), y: .value("Nodes", count))
                        .foregroundStyle(.teal.gradient).cornerRadius(2)
                }
                .chartXAxis { AxisMarks(values: .stride(by: .hour, count: 6)) { value in
                    AxisValueLabel(format: .dateTime.hour())
                } }
                .chartYAxis { AxisMarks(position: .leading) }
                .frame(height: 100)
                Text("\(snapshot.hourlyCounts().reduce(0) { $0 + $1.1 }) completed nodes · includes copies and model summaries")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack { Text("Operation latency").font(.headline); Spacer(); Text("Mean / calls").font(.caption).foregroundStyle(.secondary) }
                ForEach(["fetch", "zoom", "compact", "summarize"], id: \.self) { name in
                    HStack {
                        Text(name.capitalized)
                        Spacer()
                        if let operation = metrics.operations[name] {
                            Text(latency(operation.average)).monospacedDigit()
                            Text("\(compact(operation.count))").foregroundStyle(.secondary).monospacedDigit().frame(width: 44, alignment: .trailing)
                        } else { Text("No calls yet").foregroundStyle(.secondary) }
                    }.font(.caption)
                }
                Text("Fetch and compact include time waiting for summaries. Failed calls are included.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack { Text("Summarizer tokens").font(.headline); Spacer(); Text("\(metrics.usage_calls) reported calls").font(.caption).foregroundStyle(.secondary) }
                HStack(spacing: 8) {
                    tile("Input", compact(Int(metrics.tokens["input"] ?? 0)), "arrow.down")
                    tile("Output", compact(Int(metrics.tokens["output"] ?? 0)), "arrow.up")
                    tile("Cache read", compact(Int(metrics.tokens["cache_read"] ?? 0)), "arrow.triangle.2.circlepath")
                }
                if let writes = metrics.tokens["cache_write"], writes > 0 {
                    Text("Cache writes: \(compact(Int(writes))) tokens").font(.caption2).foregroundStyle(.secondary)
                }
                if metrics.unreported_calls > 0 {
                    Text("\(metrics.unreported_calls) calls did not report token usage.").font(.caption2).foregroundStyle(.orange)
                }
                Text("Measured since \(Date(timeIntervalSince1970: metrics.since).formatted(date: .abbreviated, time: .shortened)). Input excludes cache reads. Master-agent tokens are outside these totals.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        } else {
            Label("Telemetry starts after the updated daemon is restarted.", systemImage: "info.circle")
                .font(.caption).foregroundStyle(.secondary)
        }
        Text("Updated \(Date(timeIntervalSince1970: snapshot.timestamp).formatted(date: .omitted, time: .standard)) · refreshes every 5 seconds")
            .font(.caption2).foregroundStyle(.secondary)
    }

    func tile(_ title: String, _ value: String, _ icon: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Image(systemName: icon).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(size: 17, weight: .semibold, design: .rounded)).lineLimit(1).minimumScaleFactor(0.65)
            Text(title).font(.system(size: 9)).foregroundStyle(.secondary).lineLimit(1)
        }.frame(maxWidth: .infinity, alignment: .leading).padding(10)
            .background(.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let monitor = Monitor()
    let popover = NSPopover()
    var item: NSStatusItem!
    var viewerWindow: NSWindow?
    var layoutCheckComplete = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.target = self
        item.button?.action = #selector(toggle)
        popover.behavior = .transient
        popover.contentViewController = NSHostingController(
            rootView: Dashboard(monitor: monitor, openViewer: { [weak self] in self?.openViewer() }))
        NotificationCenter.default.addObserver(self, selector: #selector(update), name: .init("OptChatUpdated"), object: nil)
        monitor.start()
        update()
        if CommandLine.arguments.contains("--show") { toggle() }
    }

    @objc func openViewer() {
        if viewerWindow == nil {
            let window = NSWindow(contentViewController: NSHostingController(rootView: MemoryViewer(monitor: monitor)))
            window.title = "OptChat Memory"
            window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
            window.setContentSize(NSSize(width: 760, height: 620))
            window.isReleasedWhenClosed = false
            window.center()
            viewerWindow = window
        }
        NSApp.activate(ignoringOtherApps: true)
        viewerWindow?.makeKeyAndOrderFront(nil)
    }

    @objc func update() {
        let health = monitor.snapshot?.health ?? .offline
        let color: NSColor = switch health {
        case .settled: .systemGreen
        case .working: .systemBlue
        case .waiting: .systemOrange
        case .failed: .systemRed
        case .offline: .secondaryLabelColor
        }
        let icon = NSImage(size: NSSize(width: 14, height: 18), flipped: false) { _ in
            color.setFill()
            NSBezierPath(ovalIn: NSRect(x: 3, y: 5, width: 8, height: 8)).fill()
            return true
        }
        item.button?.image = icon
        item.button?.title = "OC"
        item.button?.toolTip = "OptChat · \(health.rawValue)"
        item.button?.setAccessibilityLabel("OptChat statistics: \(health.rawValue)")
        if CommandLine.arguments.contains("--layout-check"), monitor.snapshot != nil, !layoutCheckComplete {
            layoutCheckComplete = true
            toggle()
            // Let AppKit finish the popover animation before inspecting our own window.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [self] in layoutCheck() }
        }
    }

    @objc func toggle() {
        if popover.isShown { popover.performClose(nil) }
        else if let button = item.button {
            monitor.refresh()
            let available = button.window?.screen?.visibleFrame.height ?? NSScreen.main?.visibleFrame.height ?? 720
            let size = NSSize(width: 420, height: PopoverLayout.height(availableHeight: available))
            let controller = NSHostingController(
                rootView: Dashboard(monitor: monitor, height: size.height,
                                    openViewer: { [weak self] in self?.openViewer() }))
            controller.preferredContentSize = size
            popover.contentViewController = controller
            popover.contentSize = size
            NSApp.activate(ignoringOtherApps: true)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }

    func layoutCheck() {
        guard let index = CommandLine.arguments.firstIndex(of: "--layout-check"),
              CommandLine.arguments.count > index + 1,
              let view = popover.contentViewController?.view,
              let window = view.window, let screen = window.screen else { exit(2) }
        view.layoutSubtreeIfNeeded()
        let path = CommandLine.arguments[index + 1]
        let fits = window.frame.minY >= screen.visibleFrame.minY && window.frame.maxY <= screen.frame.maxY
        let diagnostics: [String: Any] = ["fits_screen": fits,
            "window": NSStringFromRect(window.frame), "visible_screen": NSStringFromRect(screen.visibleFrame),
            "hosting_bounds": NSStringFromRect(view.bounds), "content_size": NSStringFromSize(popover.contentSize),
            "health": monitor.snapshot?.health.rawValue ?? "Unknown"]
        if let data = try? JSONSerialization.data(withJSONObject: diagnostics, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: URL(fileURLWithPath: path + ".json"))
        }
        if let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
            view.cacheDisplay(in: view.bounds, to: bitmap)
            try? bitmap.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path + ".png"))
        }
        exit(fits ? 0 : 1)
    }
}

let application = NSApplication.shared
application.setActivationPolicy(.accessory)
if CommandLine.arguments.contains("--replace") {
    for existing in NSRunningApplication.runningApplications(withBundleIdentifier: "local.optchat.bar")
        where existing.processIdentifier != ProcessInfo.processInfo.processIdentifier {
        existing.terminate()
    }
}
let delegate = AppDelegate()
application.delegate = delegate
application.run()
