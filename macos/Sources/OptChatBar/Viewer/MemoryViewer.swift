import SwiftUI
import OptChatBarCore

@MainActor
final class ViewerModel: ObservableObject {
    struct Row: Identifiable {
        let line: MemoryLine
        let depth: Int
        var id: String { line.id }
    }

    @Published private(set) var payload: MemoryViewPayload?
    @Published private(set) var expanded: [String: [MemoryLine]] = [:]
    @Published private(set) var openMessages: Set<String> = []
    @Published private(set) var messages: [String: String] = [:]
    @Published private(set) var failures: [String: String] = [:]
    @Published private(set) var loading: Set<String> = []
    @Published private(set) var refreshing = false
    @Published var error: String?
    @Published var live = true

    private unowned let monitor: Monitor

    init(monitor: Monitor) {
        self.monitor = monitor
    }

    var rows: [Row] {
        rows(payload?.lines ?? [], depth: 0)
    }

    func isExpanded(_ line: MemoryLine) -> Bool {
        expanded[line.id] != nil || (line.n == 1 && openMessages.contains(line.id))
    }

    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        let python = monitor.python
        let resources = monitor.resources
        let home = monitor.home
        Task {
            let result = await Task.detached(priority: .utility) {
                Result { try ViewerProbe.view(python: python, resources: resources, home: home) }
            }.value
            refreshing = false
            guard monitor.home == home else { refresh(); return }
            switch result {
            case .success(let value): payload = value; error = nil
            case .failure(let failure): error = failure.localizedDescription
            }
        }
    }

    func toggle(_ line: MemoryLine) {
        if expanded[line.id] != nil {
            expanded[line.id] = nil
            return
        }
        if line.n == 1 {
            if openMessages.contains(line.id) {
                openMessages.remove(line.id)
            } else if messages[line.id] != nil {
                openMessages.insert(line.id)
            } else {
                load(line, showMessages: true)
            }
            return
        }
        guard !loading.contains(line.id) else { return }
        load(line, showMessages: false)
    }

    private func load(_ line: MemoryLine, showMessages: Bool) {
        loading.insert(line.id)
        failures[line.id] = nil
        let python = monitor.python
        let resources = monitor.resources
        let home = monitor.home
        let start = line.start
        let n = line.n
        Task {
            let result = await Task.detached(priority: .userInitiated) {
                Result { try ViewerProbe.zoom(python: python, resources: resources, home: home, start: start, n: n) }
            }.value
            loading.remove(line.id)
            switch result {
            case .success(let lines):
                if showMessages {
                    messages[line.id] = lines.first?.text ?? ""
                    openMessages.insert(line.id)
                } else {
                    expanded[line.id] = lines
                }
            case .failure(let failure):
                failures[line.id] = failure.localizedDescription
            }
        }
    }

    private func rows(_ lines: [MemoryLine], depth: Int) -> [Row] {
        lines.flatMap { line in
            var result = [Row(line: line, depth: depth)]
            if let children = expanded[line.id] {
                result += rows(children, depth: depth + 1)
            }
            return result
        }
    }
}

struct LineRow: View {
    let line: MemoryLine
    let depth: Int
    let expanded: Bool
    let loading: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Spacer().frame(width: CGFloat(depth) * 16)
            Image(systemName: expanded ? "chevron.down" : "chevron.right")
                .font(.system(size: 9, weight: .semibold))
                .foregroundStyle(.secondary)
                .opacity(line.built ? 1 : 0.3)
                .frame(width: 10)
            if loading {
                ProgressView().controlSize(.mini).frame(width: 10)
            }
            Text("\(line.start)+\(line.n)")
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
                .frame(width: 62, alignment: .trailing)
            Text(line.text)
                .font(.system(.caption, design: line.built ? .default : .monospaced))
                .foregroundStyle(line.built ? Color.primary : Color.secondary)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
        .padding(.vertical, 2)
    }
}

struct MemoryViewer: View {
    @ObservedObject var monitor: Monitor
    @StateObject private var model: ViewerModel
    private let timer = Timer.publish(every: 5, on: .main, in: .common).autoconnect()

    init(monitor: Monitor) {
        self.monitor = monitor
        _model = StateObject(wrappedValue: ViewerModel(monitor: monitor))
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(model.rows) { row in
                        LineRow(line: row.line, depth: row.depth,
                                expanded: model.isExpanded(row.line),
                                loading: model.loading.contains(row.line.id))
                            .onTapGesture { model.toggle(row.line) }
                        if model.openMessages.contains(row.line.id),
                           let message = model.messages[row.line.id] {
                            Text(message)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .padding(.leading, CGFloat(row.depth + 1) * 16 + 88)
                                .padding(.vertical, 4)
                        }
                        if let failure = model.failures[row.line.id] {
                            Text(failure)
                                .font(.caption).foregroundStyle(.red)
                                .padding(.leading, CGFloat(row.depth + 1) * 16 + 88)
                        }
                    }
                }.padding(10)
            }
            if let error = model.error {
                Divider()
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.caption).foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .frame(minWidth: 640, minHeight: 420)
        .onAppear { model.refresh() }
        .onReceive(timer) { _ in if model.live { model.refresh() } }
    }

    var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "text.magnifyingglass").foregroundStyle(.teal)
            if let payload = model.payload {
                Text("\(payload.messages.formatted()) messages").font(.caption).foregroundStyle(.secondary)
                Text(payload.settled ? "View ready" : "Summarizing…").font(.caption)
                Text("\(bytes(Int64(payload.view_bytes))) / \(bytes(Int64(payload.view_budget)))")
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            } else {
                Text("Loading memory…").font(.caption).foregroundStyle(.secondary)
            }
            if model.refreshing { ProgressView().controlSize(.small) }
            Spacer()
            Toggle("Live", isOn: $model.live).toggleStyle(.switch).controlSize(.small)
            Button { model.refresh() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.plain).help("Refresh memory")
        }.padding(10)
    }
}
