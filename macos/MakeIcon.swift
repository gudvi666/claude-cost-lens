import AppKit

let destination = URL(fileURLWithPath: CommandLine.arguments[1])
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let p = CGFloat(pixels)
        let rect = NSRect(x: p * 0.08, y: p * 0.08, width: p * 0.84, height: p * 0.84)
        let background = NSBezierPath(roundedRect: rect, xRadius: p * 0.19, yRadius: p * 0.19)
        NSGradient(starting: NSColor(red: 0.17, green: 0.20, blue: 0.34, alpha: 1),
                   ending: NSColor(red: 0.055, green: 0.065, blue: 0.11, alpha: 1))!.draw(in: background, angle: -55)
        for (index, height) in [0.22, 0.36, 0.51].enumerated() {
            let bar = NSBezierPath(roundedRect: NSRect(x: p * (0.25 + Double(index) * 0.18), y: p * 0.25, width: p * 0.12, height: p * height), xRadius: p * 0.035, yRadius: p * 0.035)
            NSGradient(starting: NSColor(red: 0.48, green: 0.57, blue: 1, alpha: 1),
                       ending: NSColor(red: 0.79, green: 0.48, blue: 0.95, alpha: 1))!.draw(in: bar, angle: 90)
        }
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let name = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: destination.appendingPathComponent(name))
    }
}
