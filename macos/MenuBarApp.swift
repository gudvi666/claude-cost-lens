import AppKit
import SwiftUI
import Darwin

@MainActor
final class UsageStore: ObservableObject {
    @Published var snapshot: UsageSnapshot?
    @Published var isRefreshing = false
    @Published var isStarting = false
    @Published var error: String?
    @Published var updatedAt: Date?
    @Published var showTokens = UserDefaults.standard.bool(forKey: "showTokens") {
        didSet {
            UserDefaults.standard.set(showTokens, forKey: "showTokens")
            didUpdate?()
        }
    }
    var didUpdate: (() -> Void)?
    private(set) var serverURL: URL?
    private var backend: Process?
    private var output: Pipe?
    private var timer: Timer?
    private var buffer = ""
    private var stopping = false

    var busy: Bool { isRefreshing || isStarting }

    func start() {
        startBackend()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
    }

    private func python() -> URL? {
        let environment = ProcessInfo.processInfo.environment
        var candidates: [String] = []
        if let custom = environment["CLAUDE_USAGE_PYTHON"], !custom.isEmpty {
            if custom.contains("/") { candidates.append(custom) }
            else {
                candidates += (environment["PATH"] ?? "").split(separator: ":").map { "\($0)/\(custom)" }
            }
        } else {
            candidates = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]
        }
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            let probe = Process()
            probe.executableURL = URL(fileURLWithPath: path)
            probe.arguments = ["--version"]
            probe.standardOutput = FileHandle.nullDevice
            probe.standardError = FileHandle.nullDevice
            do {
                try probe.run()
                probe.waitUntilExit()
                if probe.terminationStatus == 0 { return probe.executableURL }
            } catch { continue }
        }
        return nil
    }

    private func startBackend() {
        guard !isStarting, backend?.isRunning != true else { return }
        guard let interpreter = python() else {
            error = "未找到 Python 3。安装 Python 3 后，点击刷新重试。"
            didUpdate?()
            return
        }
        guard let resource = Bundle.main.resourceURL else { return }
        isStarting = true
        stopping = false
        error = nil
        buffer = ""
        serverURL = nil
        output?.fileHandleForReading.readabilityHandler = nil
        let child = Process()
        let pipe = Pipe()
        child.executableURL = interpreter
        child.arguments = ["-u", resource.appendingPathComponent("app.py").path,
                           "--host", "127.0.0.1", "--port", "0"]
        child.currentDirectoryURL = resource
        // The app owns this private backend; an existing npm server is untouched.
        child.standardOutput = pipe
        child.standardError = pipe
        child.terminationHandler = { [weak self] process in
            Task { @MainActor in
                guard let self, self.backend === process, !self.stopping else { return }
                self.isStarting = false
                self.isRefreshing = false
                self.serverURL = nil
                self.error = "用量服务已停止。点击刷新重新启动。"
                self.didUpdate?()
            }
        }
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in self?.receive(text) }
        }
        backend = child
        output = pipe
        do {
            try child.run()
        } catch {
            pipe.fileHandleForReading.readabilityHandler = nil
            isStarting = false
            self.error = "启动失败：\(error.localizedDescription)"
            didUpdate?()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 35) { [weak self, weak child] in
            guard let self, let child, self.backend === child, self.serverURL == nil, self.isStarting else { return }
            self.stopping = true
            self.isStarting = false
            self.error = "读取用量超时。请确认 ~/.claude 可访问，然后点击刷新。"
            if child.isRunning { child.terminate() }
            self.didUpdate?()
        }
    }

    private func receive(_ text: String) {
        buffer += text
        if buffer.count > 32_768 { buffer = String(buffer.suffix(16_384)) }
        guard serverURL == nil else { return }
        for line in buffer.components(separatedBy: .newlines) {
            let prefix = "Claude Cost Lens: "
            guard line.hasPrefix(prefix), line.hasSuffix("/"), let url = URL(string: String(line.dropFirst(prefix.count))),
                  url.host == "127.0.0.1", let port = url.port, port > 0 else { continue }
            serverURL = url
            isStarting = false
            refresh()
            break
        }
    }

    func refresh() {
        guard !busy else { return }
        guard let base = serverURL else { startBackend(); return }
        isRefreshing = true
        Task {
            defer { isRefreshing = false; didUpdate?() }
            do {
                var request = URLRequest(url: base.appendingPathComponent("api/summary"))
                request.timeoutInterval = 25
                request.cachePolicy = .reloadIgnoringLocalCacheData
                let (data, response) = try await URLSession.shared.data(for: request)
                guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                    throw URLError(.badServerResponse)
                }
                snapshot = try JSONDecoder().decode(UsageSnapshot.self, from: data)
                updatedAt = Date()
                error = nil
            } catch {
                self.error = "刷新失败，保留上次数据。\(error.localizedDescription)"
            }
        }
    }

    func openDashboard() {
        if let url = serverURL { NSWorkspace.shared.open(url) }
        else { refresh() }
    }

    func stop() {
        stopping = true
        timer?.invalidate()
        output?.fileHandleForReading.readabilityHandler = nil
        if backend?.isRunning == true { backend?.terminate() }
    }
}

private let accent = Color(red: 0.57, green: 0.64, blue: 1)
private let muted = Color(red: 0.58, green: 0.64, blue: 0.75)
private let gold = Color(red: 1, green: 0.79, blue: 0.47)

// All geometry uses screen points, including screens with a negative origin.
enum PopoverGeometry {
    static func contentSize(in visibleFrame: NSRect) -> NSSize {
        NSSize(width: min(408, max(1, visibleFrame.width - 32)),
               height: min(820, max(1, visibleFrame.height - 48)))
    }

    static func constrainedFrame(_ frame: NSRect, in visibleFrame: NSRect) -> NSRect {
        let safe = visibleFrame.insetBy(dx: 8, dy: 8)
        var result = frame
        result.origin.x = min(max(frame.minX, safe.minX), max(safe.minX, safe.maxX - frame.width))
        result.origin.y = min(max(frame.minY, safe.minY), max(safe.minY, safe.maxY - frame.height))
        return result
    }
}

@MainActor
final class PopoverLayout: ObservableObject {
    @Published var size = NSSize(width: 408, height: 820)
}

struct UsagePopover: View {
    @ObservedObject var store: UsageStore
    @ObservedObject var layout: PopoverLayout
    @State private var range = 1

    init(store: UsageStore, layout: PopoverLayout, initialRange: Int = 1) {
        self.store = store
        self.layout = layout
        _range = State(initialValue: initialRange)
    }

    private var metrics: UsageSnapshot.Metrics? { store.snapshot?.metrics(days: range) }
    private var rangeName: String { range == 0 ? "全部本地" : range == 1 ? "今日" : "近 \(range) 天" }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 14) {
                header
                Picker("时间范围", selection: $range) {
                    Text("今天").tag(1)
                    Text("近 7 天").tag(7)
                    Text("近 30 天").tag(30)
                    Text("全部").tag(0)
                }
                .pickerStyle(.segmented).labelsHidden()
            }
            .padding(.horizontal, 20).padding(.top, 20).padding(.bottom, 14)
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                hero
                tokenDetails
                if let snapshot = store.snapshot {
                    modelSection(snapshot)
                    HStack(spacing: 8) {
                        Label("\(snapshot.session_count) 个会话", systemImage: "bubble.left.and.bubble.right")
                        Spacer()
                        Text("\(UsageFormat.exact(Int64(snapshot.usage_event_count))) 个请求")
                    }
                    .font(.system(size: 11)).foregroundStyle(muted)
                    if let count = snapshot.cost_estimated_session_count, count > 0 {
                        Text("\(count) 个会话使用 API 估算 · \(snapshot.cost_estimate_partial_session_count ?? 0) 个部分估算")
                            .help("原生记录可用后自动替换；部分估算表示某些调用缺少用量或价格。")
                            .font(.system(size: 11)).foregroundStyle(gold.opacity(0.85))
                    } else if snapshot.cost_missing_session_count > 0 {
                        Text("\(snapshot.cost_missing_session_count) 个会话尚无可用费用记录。")
                            .font(.system(size: 11)).foregroundStyle(gold.opacity(0.85))
                    }
                    if let count = snapshot.cost_unreconciled_session_count, count > 0 {
                        Text("累计 \(count) 个会话原生基数待对账；估算仅含已知日志。")
                            .font(.system(size: 10)).foregroundStyle(muted)
                    }
                }
                }
                .padding(.horizontal, 20).padding(.top, 4).padding(.bottom, 20)
            }
            .frame(minHeight: 0, maxHeight: .infinity)
            .clipped()
            footer.padding(.horizontal, 20).padding(.top, 12).padding(.bottom, 16)
                .background(Color.white.opacity(0.025))
        }
        .frame(width: layout.size.width, height: layout.size.height)
        .background(LinearGradient(colors: [Color(red: 0.09, green: 0.11, blue: 0.18), Color(red: 0.045, green: 0.055, blue: 0.09)], startPoint: .topLeading, endPoint: .bottomTrailing))
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        HStack(spacing: 11) {
            Image(systemName: "chart.bar.xaxis")
                .font(.system(size: 22, weight: .semibold)).foregroundStyle(accent)
                .frame(width: 44, height: 44).background(accent.opacity(0.13), in: RoundedRectangle(cornerRadius: 12))
            VStack(alignment: .leading, spacing: 4) {
                Text("Claude Cost Lens").font(.system(size: 18, weight: .bold))
                HStack(spacing: 5) {
                    Circle().fill(store.error == nil ? Color.green : gold).frame(width: 5, height: 5)
                    Text(store.isStarting ? "正在读取本地用量" : "本地统计 · 只读")
                        .font(.system(size: 11)).foregroundStyle(muted)
                }
            }
            Spacer()
            Menu {
                Toggle("菜单栏显示今日 Token", isOn: $store.showTokens)
                Button("打开完整面板") { store.openDashboard() }
                Divider()
                Button("退出 Claude Cost Lens") { NSApp.terminate(nil) }
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 17, weight: .semibold)).frame(width: 25, height: 28)
            }
            .menuStyle(.borderlessButton).fixedSize()
        }
    }

    private var hero: some View {
        VStack(alignment: .leading, spacing: 13) {
            HStack {
                Text("\(rangeName)费用合计" + (metrics?.hasEstimates == true ? " · 含 API 估算" : " · 原生已保存"))
                    .font(.system(size: 12, weight: .medium)).foregroundStyle(muted)
                Spacer()
                Image(systemName: "sparkle").foregroundStyle(gold)
            }
            Text(metrics?.costText ?? "—")
                .font(.system(size: 39, weight: .bold, design: .rounded)).monospacedDigit().foregroundStyle(gold)
            HStack(alignment: .firstTextBaseline) {
                Text(metrics.map { UsageFormat.tokens($0.tokens.total) } ?? "—")
                    .font(.system(size: 22, weight: .semibold, design: .rounded)).monospacedDigit()
                Text("Token").font(.system(size: 12)).foregroundStyle(muted)
                Spacer()
                Text("含 Advisor").font(.system(size: 10, weight: .medium)).foregroundStyle(accent)
                    .padding(.horizontal, 8).padding(.vertical, 4).background(accent.opacity(0.12), in: Capsule())
            }
            .help(metrics.map { "Token 合计：\(UsageFormat.tokens($0.tokens.total))\nThinking 已包含在输出中，不重复相加。" } ?? "正在读取")
            if let metrics, metrics.hasEstimates {
                Text("原生 \(UsageFormat.money(metrics.nativeCost)) + API 估算 \(UsageFormat.money(metrics.estimatedCost))")
                    .font(.system(size: 10)).foregroundStyle(gold.opacity(0.8))
                    .help("按 Claude 官方 API 价格估算（2026-10-19 核对），包含已记录的 Advisor 用量；不是订阅账单。")
            } else if let metrics, metrics.pendingSessions > 0 {
                Text("\(range == 0 ? "全部本地" : "所选日期内") \(metrics.pendingSessions) 个会话费用待记录")
                    .font(.system(size: 10)).foregroundStyle(gold.opacity(0.8))
            }
            if let metrics, metrics.unreconciledSessions > 0 {
                Text("\(metrics.unreconciledSessions) 个会话原生基数待对账")
                    .font(.system(size: 10)).foregroundStyle(gold.opacity(0.8))
            }
        }
        .padding(17).frame(maxWidth: .infinity, alignment: .leading)
        .background(LinearGradient(colors: [accent.opacity(0.14), Color.white.opacity(0.025)], startPoint: .topLeading, endPoint: .bottomTrailing), in: RoundedRectangle(cornerRadius: 16))
        .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(Color.white.opacity(0.08)))
    }

    private var tokenDetails: some View {
        let t = metrics?.tokens
        return LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
            tokenTile("输入", value: t?.inputTokens, symbol: "arrow.down.left")
            tokenTile("输出", value: t?.outputTokens, symbol: "arrow.up.right")
            tokenTile("缓存读取", value: t?.cacheReadInputTokens, symbol: "tray.and.arrow.down")
            tokenTile("缓存写入", value: t?.cacheCreationInputTokens, symbol: "tray.and.arrow.up")
        }
    }

    private func tokenTile(_ label: String, value: Int64?, symbol: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(label, systemImage: symbol).font(.system(size: 11)).foregroundStyle(muted)
            Text(value.map(UsageFormat.tokens) ?? "—").font(.system(size: 16, weight: .semibold, design: .rounded)).monospacedDigit()
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 11))
        .help(value.map { "\(label)：\(UsageFormat.tokens($0)) Token" } ?? "正在读取")
    }

    private func modelSection(_ snapshot: UsageSnapshot) -> some View {
        let models = Array(snapshot.models.sorted { $0.tokens.total > $1.tokens.total }.prefix(3))
        let maxTokens = max(models.first?.tokens.total ?? 0, 1)
        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("累计模型用量").font(.system(size: 12, weight: .semibold))
                Spacer()
                Text(UsageFormat.tokens(snapshot.totals.tokens.total)).font(.system(size: 11)).foregroundStyle(muted)
            }
            ForEach(models) { model in
                VStack(spacing: 5) {
                    HStack {
                        Text(UsageFormat.model(model.model)).font(.system(size: 11))
                        Spacer()
                        Text(UsageFormat.tokens(model.tokens.total)).font(.system(size: 11, design: .monospaced)).foregroundStyle(muted)
                    }
                    GeometryReader { geometry in
                        Capsule().fill(Color.white.opacity(0.06))
                            .overlay(alignment: .leading) {
                                Capsule().fill(LinearGradient(colors: [accent, Color.purple], startPoint: .leading, endPoint: .trailing))
                                    .frame(width: geometry.size.width * Double(model.tokens.total) / Double(maxTokens))
                            }
                    }.frame(height: 4)
                }
                .help("\(UsageFormat.model(model.model))\n累计 Token：\(UsageFormat.tokens(model.tokens.total))\n原生费用：\(UsageFormat.money(model.cost))\nAPI 估算：\(UsageFormat.money(model.estimated_cost ?? 0))")
            }
        }
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let error = store.error {
                Text(error).font(.system(size: 11)).foregroundStyle(gold).textSelection(.enabled)
            }
            HStack {
                Button { store.refresh() } label: {
                    HStack(spacing: 6) {
                        if store.busy { ProgressView().controlSize(.mini) }
                        else { Image(systemName: "arrow.clockwise") }
                        Text(store.busy ? "读取中" : "刷新")
                    }
                }.disabled(store.busy)
                Spacer()
                Button("打开完整面板 ↗") { store.openDashboard() }.disabled(store.serverURL == nil)
                    .buttonStyle(.borderedProminent).tint(accent.opacity(0.8))
            }.controlSize(.small)
            HStack {
                Text("每分钟自动刷新")
                Spacer()
                if let date = store.updatedAt { Text(date, style: .time) }
            }.font(.system(size: 10)).foregroundStyle(muted)
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private let store = UsageStore()
    private var item: NSStatusItem!
    private let popover = NSPopover()
    private let layout = PopoverLayout()
    private var terminationSignal: DispatchSourceSignal?
    private var outsideClickMonitor: Any?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        signal(SIGTERM, SIG_IGN)
        let termination = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        termination.setEventHandler { NSApp.terminate(nil) }
        termination.resume()
        terminationSignal = termination
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.image = NSImage(systemSymbolName: "chart.bar.xaxis", accessibilityDescription: "Claude 用量")
            button.imagePosition = .imageLeading
            button.font = .monospacedDigitSystemFont(ofSize: 12, weight: .medium)
            button.title = " …"
            button.target = self
            button.action = #selector(togglePopover)
        }
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(activeSpaceDidChange),
            name: NSWorkspace.activeSpaceDidChangeNotification, object: nil)
        popover.behavior = .transient
        popover.appearance = NSAppearance(named: .darkAqua)
        popover.delegate = self
        let host = NSHostingController(rootView: UsagePopover(store: store, layout: layout))
        // The popover owns its size. Do not let SwiftUI's ideal/minimum size
        // recenter a taller root view inside a screen-constrained AppKit window.
        host.sizingOptions = []
        host.preferredContentSize = layout.size
        host.view.setFrameSize(layout.size)
        popover.contentViewController = host
        popover.contentSize = layout.size
        store.didUpdate = { [weak self] in self?.updateStatus() }
        store.start()
    }

    private func updateStatus() {
        guard let snapshot = store.snapshot else {
            item.button?.title = store.error == nil ? " …" : " !"
            item.button?.toolTip = store.error ?? "正在读取 Claude 用量"
            return
        }
        let today = snapshot.metrics(days: 1)
        item.button?.title = " " + (store.showTokens ? UsageFormat.tokens(today.tokens.total) : today.costText)
        item.button?.toolTip = "今日费用合计：\(today.costText)\n原生：\(UsageFormat.money(today.nativeCost))；API 估算：\(UsageFormat.money(today.estimatedCost))\n今日 Token：\(UsageFormat.tokens(today.tokens.total))（含 Advisor）" + (store.error == nil ? "" : "\n刷新失败，当前为上次数据")
    }

    @objc private func togglePopover() {
        guard let button = item.button else { return }
        if popover.isShown { popover.performClose(nil) }
        else {
            let screen = button.window?.screen ?? NSScreen.main
            if let screen {
                layout.size = PopoverGeometry.contentSize(in: screen.visibleFrame)
                popover.contentViewController?.preferredContentSize = layout.size
                popover.contentViewController?.view.setFrameSize(layout.size)
                popover.contentSize = layout.size
            }
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func closePopover() {
        if popover.isShown { popover.performClose(nil) }
    }

    func applicationDidResignActive(_ notification: Notification) { closePopover() }

    @objc private func activeSpaceDidChange(_ notification: Notification) { closePopover() }

    func popoverDidShow(_ notification: Notification) {
        // A status item can remain visible without our app becoming active.
        // Mouse-only monitoring covers outside clicks in that state as well;
        // it neither intercepts events nor requires keyboard/accessibility access.
        removeOutsideClickMonitor()
        outsideClickMonitor = NSEvent.addGlobalMonitorForEvents(
            matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]
        ) { [weak self] _ in
            self?.closePopover()
        }
        guard let window = popover.contentViewController?.view.window,
              let screen = item.button?.window?.screen ?? window.screen else { return }
        // AppKit chooses the arrow placement; keep the resulting outer frame
        // inside this display's usable area, including popover chrome.
        let frame = PopoverGeometry.constrainedFrame(window.frame, in: screen.visibleFrame)
        if frame.origin != window.frame.origin { window.setFrameOrigin(frame.origin) }
    }

    private func removeOutsideClickMonitor() {
        if let monitor = outsideClickMonitor {
            NSEvent.removeMonitor(monitor)
            outsideClickMonitor = nil
        }
    }

    func popoverDidClose(_ notification: Notification) { removeOutsideClickMonitor() }

    func applicationWillTerminate(_ notification: Notification) {
        removeOutsideClickMonitor()
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        store.stop()
    }
}

#if !PREVIEW
@main
enum MenuBarApplication {
    @MainActor static func main() {
        let application = NSApplication.shared
        let delegate = AppDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}
#endif
