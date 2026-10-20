import AppKit
import SwiftUI

@MainActor
final class PresentationObserver: NSObject, NSPopoverDelegate {
    var didShow = false
    var didClose = false

    func popoverDidShow(_ notification: Notification) { didShow = true }
    func popoverDidClose(_ notification: Notification) { didClose = true }
}

@main
enum PopoverPresentationTests {
    @MainActor static func waitFor(_ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(2)
        while !condition() && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.01))
        }
        precondition(condition(), "Popover presentation did not complete")
    }

    @MainActor static func main() throws {
        NSApplication.shared.setActivationPolicy(.accessory)
        let data = try JSONSerialization.data(withJSONObject: ["version": 2, "samples": [
            ["t": Date().addingTimeInterval(-86400).timeIntervalSince1970 * 1000,
             "org": "fixture", "u": ["fh": 24, "sd": 35]]
        ]])
        var reads = 0
        let store = UsageStore(planUsageReader: {
            reads += 1
            return PlanUsage.decode(data)
        })
        // The manual action must check quotas even when transcript refresh is
        // busy. Keep it busy here so this test never starts a real backend.
        store.isStarting = true
        store.refresh()
        precondition(reads == 1 && store.planUsage.fiveHour().number == "76")
        store.refresh()
        precondition(reads == 2 && store.planUsage.sevenDay().number == "65")
        store.isStarting = false
        print("PASS: manual refresh reads the quota file before checking backend busy state")

        guard let screen = NSScreen.main else { preconditionFailure("Requires a macOS display") }
        let visible = screen.visibleFrame
        let anchorWindow = NSWindow(contentRect: NSRect(x: visible.midX - 50, y: visible.maxY - 26,
                                                       width: 100, height: 22),
                                    styleMask: [.borderless], backing: .buffered, defer: false)
        let anchor = NSButton(frame: NSRect(x: 0, y: 0, width: 100, height: 22))
        anchorWindow.contentView = anchor
        anchorWindow.orderFront(nil)
        defer { anchorWindow.orderOut(nil) }
        let layout = PopoverLayout()
        let popover = NSPopover()
        let host = NSHostingController(rootView: UsagePopover(store: store, layout: layout))
        host.sizingOptions = []
        popover.contentViewController = host
        popover.behavior = .applicationDefined
        let observer = PresentationObserver()
        popover.delegate = observer

        for opening in 1...3 {
            observer.didShow = false
            observer.didClose = false
            PopoverPresentation.show(popover, relativeTo: anchor, layout: layout, visibleFrame: visible)
            precondition(popover.animates)
            waitFor { observer.didShow }
            precondition(popover.isShown)
            guard let window = host.view.window else { preconditionFailure("Missing popover window") }
            let first = window.frame
            precondition(first.minY >= screen.frame.minY && first.maxY <= screen.frame.maxY)
            for tick in 0..<12 {
                if tick == 3 {
                    store.planUsage = .missing
                    store.isRefreshing = true
                } else if tick == 6 {
                    store.refreshPlanUsage()
                    store.isRefreshing = false
                }
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
                let current = window.frame
                precondition(abs(current.minY - first.minY) < 0.5 && abs(current.minX - first.minX) < 0.5 &&
                             abs(current.height - first.height) < 0.5,
                             "Popover moved after display: \(first) → \(current)")
            }
            print("PASS: animated opening \(opening) stays at \(first) after presentation and through quota and refresh UI updates")
            popover.performClose(nil)
            waitFor { observer.didClose }
        }
    }
}
