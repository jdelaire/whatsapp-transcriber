import AppKit
import SwiftUI

@main
struct TranscriberApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra {
            PanelView(model: delegate.model)
        } label: {
            Image(systemName: delegate.model.updates.updateAvailable ? "arrow.down.circle.fill" : delegate.model.snapshot.paused ? "pause.circle" : "waveform")
                .accessibilityLabel(delegate.model.updates.updateAvailable ? "Transcriber update available" : "Transcriber")
        }
        .menuBarExtraStyle(.window)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    private var terminating = false
    private var terminationReady = false
    private var previewWindow: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(didWake), name: NSWorkspace.didWakeNotification, object: nil)
        if ProcessInfo.processInfo.arguments.contains("--preview"), ProcessInfo.processInfo.arguments.contains("--preview-panel") {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 560), styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Transcriber Preview"
            window.contentViewController = NSHostingController(rootView: PanelView(model: model))
            window.center()
            window.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate()
            previewWindow = window
        }
        model.start()
        #if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("--preview"), let index = arguments.firstIndex(of: "--export-previews"), arguments.indices.contains(index + 1) {
            let directory = URL(fileURLWithPath: arguments[index + 1])
            Task {
                do { try await exportPreviews(to: directory) }
                catch { FileHandle.standardError.write(Data("Preview export failed: \(error.localizedDescription)\n".utf8)) }
                NSApplication.shared.terminate(nil)
            }
        }
        #endif
    }

    func applicationDidBecomeActive(_ notification: Notification) { model.wakeSummaries() }

    @objc private func didWake(_ notification: Notification) { model.wakeSummaries() }

    #if DEBUG
    private func exportPreviews(to directory: URL) async throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let states: [(String, @MainActor () -> Void)] = [
            ("inbox", { self.model.snapshot = .preview; self.model.route = .inbox }),
            ("paused", { self.model.togglePause() }),
            ("transcript", { self.model.openTranscript(id: self.model.snapshot.recent[0].id) }),
            ("original", { self.model.transcriptVisit.select(.original) }),
            ("setup", { self.model.snapshot.authenticated = false; self.model.snapshot.modelReady = false; self.model.route = .setup }),
            ("linking", { self.model.snapshot.modelReady = true; self.model.linkPhone() }),
            ("settings", { self.model.snapshot = .preview; self.model.route = .settings }),
            ("summary-pending", { self.model.snapshot.recent[0].summary = .init(); self.model.openTranscript(id: self.model.snapshot.recent[0].id) }),
            ("summary-failed", { self.model.snapshot.recent[0].summary = .init(state: .failed, errorCode: "generation_failed"); self.model.openTranscript(id: self.model.snapshot.recent[0].id) }),
            ("summary-unavailable", { self.model.summaryAvailability = .unavailable(.modelNotReady); self.model.route = .settings }),
            ("summary-off", { self.model.snapshot = .preview; self.model.snapshot.summarySettings.enabled = false; self.model.summaryAvailability = .available; self.model.route = .settings })
        ]
        for (name, update) in states {
            update()
            try await Task.sleep(for: .milliseconds(250))
            guard let view = previewWindow?.contentView else { throw CocoaError(.fileNoSuchFile) }
            previewWindow?.setContentSize(view.fittingSize)
            view.layoutSubtreeIfNeeded()
            guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw CocoaError(.fileWriteUnknown) }
            view.cacheDisplay(in: view.bounds, to: bitmap)
            guard let data = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
            try data.write(to: directory.appendingPathComponent("\(name).png"))
        }
    }
    #endif

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminationReady { return .terminateNow }
        if terminating { return .terminateCancel }
        terminating = true
        Task {
            await model.shutdown()
            terminationReady = true
            sender.terminate(nil)
        }
        return .terminateCancel
    }
}
