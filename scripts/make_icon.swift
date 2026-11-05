import AppKit

let directory = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let context = NSGraphicsContext.current!.cgContext
        context.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
        let background = NSBezierPath(roundedRect: NSRect(x: 54, y: 54, width: 916, height: 916), xRadius: 208, yRadius: 208)
        NSGradient(starting: NSColor(srgbRed: 0.12, green: 0.68, blue: 0.49, alpha: 1), ending: NSColor(srgbRed: 0.05, green: 0.43, blue: 0.36, alpha: 1))!.draw(in: background, angle: -90)
        NSColor.white.setFill()
        for (index, height) in [130.0, 240, 340, 200, 110].enumerated() {
            NSBezierPath(roundedRect: NSRect(x: 272 + Double(index) * 94, y: 540 - height / 2, width: 52, height: height), xRadius: 26, yRadius: 26).fill()
        }
        NSColor.white.withAlphaComponent(0.9).setFill()
        NSBezierPath(roundedRect: NSRect(x: 288, y: 294, width: 448, height: 35), xRadius: 17.5, yRadius: 17.5).fill()
        NSBezierPath(roundedRect: NSRect(x: 288, y: 216, width: 286, height: 35), xRadius: 17.5, yRadius: 17.5).fill()
        image.unlockFocus()
        let representation = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let data = representation.representation(using: .png, properties: [:])!
        let suffix = scale == 2 ? "@2x" : ""
        try data.write(to: directory.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
