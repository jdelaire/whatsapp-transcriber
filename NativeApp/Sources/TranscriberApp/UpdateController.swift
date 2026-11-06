import AppKit
import Observation
import Sparkle

@MainActor @Observable
final class UpdateController: NSObject, @preconcurrency SPUStandardUserDriverDelegate {
    private(set) var canCheckForUpdates = false
    private(set) var automaticChecks = true
    private(set) var updateAvailable = false
    private var controller: SPUStandardUpdaterController?
    private var observations: [NSKeyValueObservation] = []

    func start() {
        guard controller == nil,
              !ProcessInfo.processInfo.arguments.contains("--preview"),
              ProcessInfo.processInfo.environment["TRANSCRIBER_DATA_ROOT"] == nil else { return }
        let controller = SPUStandardUpdaterController(startingUpdater: false, updaterDelegate: nil, userDriverDelegate: self)
        self.controller = controller
        observations = [
            controller.updater.observe(\.canCheckForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
                MainActor.assumeIsolated { self?.canCheckForUpdates = updater.canCheckForUpdates }
            },
            controller.updater.observe(\.automaticallyChecksForUpdates, options: [.initial, .new]) { [weak self] updater, _ in
                MainActor.assumeIsolated { self?.automaticChecks = updater.automaticallyChecksForUpdates }
            }
        ]
        controller.startUpdater()
    }

    func checkForUpdates() {
        guard canCheckForUpdates else { return }
        NSApplication.shared.activate()
        controller?.checkForUpdates(nil)
    }

    func setAutomaticChecks(_ enabled: Bool) {
        guard let controller else { return }
        controller.updater.automaticallyChecksForUpdates = enabled
    }

    var supportsGentleScheduledUpdateReminders: Bool { true }

    func standardUserDriverWillHandleShowingUpdate(_ handleShowingUpdate: Bool, forUpdate update: SUAppcastItem, state: SPUUserUpdateState) {
        updateAvailable = !state.userInitiated
    }

    func standardUserDriverDidReceiveUserAttention(forUpdate update: SUAppcastItem) {
        updateAvailable = false
    }

    func standardUserDriverWillFinishUpdateSession() {
        updateAvailable = false
    }
}
