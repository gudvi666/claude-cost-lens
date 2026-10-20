// Render our own SwiftUI view into an artifact without opening a desktop window.
import AppKit
import SwiftUI

@main
enum RenderPreview {
    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let store = UsageStore()
        defer { store.stop() }
        if CommandLine.arguments[1] == "--live" {
            store.start()
            let deadline = Date().addingTimeInterval(30)
            while store.snapshot == nil && store.error == nil && Date() < deadline {
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            }
            guard store.snapshot != nil else {
                throw NSError(domain: "MenuBarPreview", code: 1, userInfo: [NSLocalizedDescriptionKey: store.error ?? "Live backend timed out"])
            }
        } else {
            store.snapshot = try JSONDecoder().decode(UsageSnapshot.self, from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
            store.updatedAt = Date()
        }
        if CommandLine.arguments.contains("--quota-live") {
            store.refreshPlanUsage()
        } else if !CommandLine.arguments.contains("--quota-missing") {
            let age: TimeInterval = CommandLine.arguments.contains("--quota-old") ? 86400 : 0
            let used = CommandLine.arguments.contains("--quota-empty") ? 100 : 24
            let sample: [String: Any] = ["version": 2, "samples": [
                ["t": Date().addingTimeInterval(-age).timeIntervalSince1970 * 1000,
                 "org": "preview", "u": ["fh": used, "sd": 35]]
            ]]
            store.planUsage = PlanUsage.decode(try JSONSerialization.data(withJSONObject: sample))
        }
        if let index = CommandLine.arguments.firstIndex(of: "--status-output") {
            let image = StatusQuotaImage.make(store.planUsage)
            guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff) else {
                preconditionFailure("Unable to render menu bar quota image")
            }
            try bitmap.representation(using: .png, properties: [:])!.write(
                to: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
        }
        let layout = PopoverLayout()
        if CommandLine.arguments.count > 3, let height = Double(CommandLine.arguments[3]) {
            layout.size = PopoverGeometry.contentSize(in: NSRect(x: 0, y: 0, width: 1024, height: height))
        }
        let host = NSHostingController(rootView: UsagePopover(store: store, layout: layout,
            initialRange: CommandLine.arguments.contains("--all") ? 0 : 1))
        host.sizingOptions = []
        host.preferredContentSize = layout.size
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: layout.size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentViewController = host
        host.view.frame = NSRect(origin: .zero, size: layout.size)
        host.view.layoutSubtreeIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.3))
        host.view.layoutSubtreeIfNeeded()
        if CommandLine.arguments.contains("--bottom") {
            func findScrollView(_ view: NSView) -> NSScrollView? {
                if let scroll = view as? NSScrollView { return scroll }
                return view.subviews.compactMap(findScrollView).first
            }
            guard let scroll = findScrollView(host.view), let document = scroll.documentView else {
                preconditionFailure("Missing middle scroll region")
            }
            let bottom = document.isFlipped ? max(0, document.bounds.height - scroll.contentView.bounds.height) : 0
            scroll.contentView.scroll(to: NSPoint(x: 0, y: bottom))
            scroll.reflectScrolledClipView(scroll.contentView)
            RunLoop.current.run(until: Date().addingTimeInterval(0.1))
        }
        host.view.displayIfNeeded()
        // Composite the layer tree: cacheDisplay ignores clipping masks while
        // drawing scrolled native subviews and can produce a misleading image.
        guard let layer = host.view.layer else { preconditionFailure("Missing hosting layer") }
        let scale: CGFloat = 2
        let width = Int(layout.size.width * scale)
        let height = Int(layout.size.height * scale)
        let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.translateBy(x: 0, y: CGFloat(height))
        context.scaleBy(x: scale, y: -scale)
        layer.render(in: context)
        let bitmap = NSBitmapImageRep(cgImage: context.makeImage()!)
        try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
        precondition(host.view.bounds.size == layout.size, "Hosting view must match the popover content size")
        print("Rendered native SwiftUI preview: \(host.view.bounds.size)")
    }
}
