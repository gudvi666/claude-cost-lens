import AppKit
import SwiftUI

@main
enum PopoverLayoutTests {
    @MainActor static func main() {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let store = UsageStore()
        // Cover a laptop, a short usable display, and a second display whose
        // desktop coordinates are negative. Include the popover arrow/border.
        let screens = [
            NSRect(x: 0, y: 90, width: 2560, height: 1320),
            NSRect(x: 0, y: 40, width: 1024, height: 560),
            NSRect(x: -1920, y: -200, width: 1920, height: 1040),
            NSRect(x: 0, y: 20, width: 800, height: 400)
        ]
        for screen in screens {
            let layout = PopoverLayout()
            layout.size = PopoverGeometry.contentSize(in: screen)
            let outer = NSRect(x: screen.maxX - 180, y: screen.maxY - 100,
                               width: layout.size.width + 4, height: layout.size.height + 24)
            let fitted = PopoverGeometry.constrainedFrame(outer, in: screen)
            precondition(screen.insetBy(dx: 8, dy: 8).contains(fitted), "Outer popover clips the display: \(screen)")
            let host = NSHostingController(rootView: UsagePopover(store: store, layout: layout))
            host.sizingOptions = []
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: layout.size),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentViewController = host
            host.view.frame = NSRect(origin: .zero, size: layout.size)
            host.view.layoutSubtreeIfNeeded()
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
            precondition(host.view.bounds.size == layout.size, "SwiftUI changed constrained content size")
            precondition(window.contentMinSize.height <= layout.size.height, "Host minimum forces content off screen")
            print("PASS display \(screen) → content \(layout.size), outer \(fitted)")
        }
    }
}
