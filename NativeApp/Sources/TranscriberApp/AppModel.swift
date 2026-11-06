import AppKit
import Observation
import ServiceManagement
import UserNotifications
import TranscriberCore

enum PanelRoute: Equatable {
    case inbox, setup, settings, detail(Transcript)
}

@MainActor @Observable
final class AppModel {
    let updates = UpdateController()
    var snapshot = EngineSnapshot.empty
    var route: PanelRoute = .inbox
    var isBusy = false
    var error: String?
    var notice: String?
    var pairingCode: String?
    var phone = ""
    var downloading = false
    var downloadProgress = 0.0
    var launchAtLogin = false
    var notifications = UserDefaults.standard.bool(forKey: "notifyWhenReady")
    var panelVisible = false
    private(set) var shuttingDown = false
    private let preview: Bool
    private let isolated: Bool
    private let location: EngineLocation
    private let client: EngineClient
    private var worker: EngineProcess?
    private var pairing: EngineProcess?
    private var pairingToken: UUID?
    private var workerReady = false
    private var workerToken: UUID?
    private var monitor: Task<Void, Never>?
    private var startup: Task<Void, Never>?
    private var operation: Task<Void, Never>?
    private var downloader: ModelDownload?
    private var refreshing = false

    init() {
        preview = ProcessInfo.processInfo.arguments.contains("--preview")
        let root = ProcessInfo.processInfo.environment["TRANSCRIBER_DATA_ROOT"].map { URL(fileURLWithPath: $0) }
        isolated = root != nil
        location = EngineLocation(bundle: Bundle.main.bundleURL, dataRoot: root)
        client = EngineClient(location: location)
        if preview {
            snapshot = .preview
            if ProcessInfo.processInfo.arguments.contains("--preview-setup") {
                snapshot.authenticated = false
                snapshot.modelReady = false
                route = .setup
            }
        }
    }

    var phase: CapturePhase { snapshot.phase }
    var statusTitle: String { pairing != nil ? "Linking WhatsApp" : downloading ? "Downloading speech model" : phase.title }
    var canCapture: Bool { snapshot.modelReady && snapshot.authenticated && snapshot.authenticationError == nil }

    func start() {
        updates.start()
        startup = Task { [weak self] in await self?.bootstrap() }
    }

    private func bootstrap() async {
        if preview { return }
        isBusy = true
        defer { isBusy = false }
        do {
            snapshot = try await client.snapshot(command: "initialize")
            try Task.checkCancellation()
            guard !shuttingDown else { return }
            launchAtLogin = !isolated && SMAppService.mainApp.status == .enabled
            if canCapture && !snapshot.paused { try await startCapture() }
            else if !canCapture { route = .setup }
            try Task.checkCancellation()
            monitor = Task { [weak self] in
                while !Task.isCancelled {
                    guard let self else { return }
                    if !self.isBusy && !self.downloading && self.pairing == nil { await self.refresh() }
                    try? await Task.sleep(for: self.panelVisible ? .seconds(3) : .seconds(20))
                }
            }
        } catch is CancellationError {}
        catch { self.error = error.localizedDescription }
    }

    func refresh() async {
        guard !preview, !refreshing, !shuttingDown else { return }
        refreshing = true
        defer { refreshing = false }
        do { snapshot = try await client.snapshot() }
        catch { self.error = error.localizedDescription }
    }

    private func perform(_ work: @escaping @MainActor () async throws -> Void) {
        guard !isBusy, !shuttingDown else { return }
        isBusy = true
        error = nil
        operation = Task { [weak self] in
            do { try await work() }
            catch is CancellationError {}
            catch { self?.error = error.localizedDescription }
            self?.isBusy = false
            self?.operation = nil
        }
    }

    func togglePause() {
        if preview { snapshot.paused.toggle(); return }
        perform { [self] in
            if snapshot.paused {
                try await client.action("resume")
                await refresh()
                try await startCapture()
            } else {
                try await client.action("pause")
                snapshot.paused = true
                await stopWorker()
                await refresh()
            }
        }
    }

    func resume() {
        if preview { snapshot.paused = false; return }
        perform { [self] in
            try await client.action("resume")
            await refresh()
            try await startCapture()
        }
    }

    private func startCapture() async throws {
        guard worker == nil, !shuttingDown else { return }
        let migrating = snapshot.migrationNeeded || ["prepared", "disabled"].contains(snapshot.migrationPhase)
        do {
            if migrating { try await client.action("migrate") }
            try Task.checkCancellation()
            guard !shuttingDown else { throw CancellationError() }
            workerReady = false
            let token = UUID()
            workerToken = token
            let process = EngineProcess(location: location, command: "run")
            worker = process
            Task.detached(priority: .utility) { [weak self] in
                do {
                    let result = try process.stream { [weak self] event in
                        Task { @MainActor in self?.receive(event, token: token) }
                    }
                    await self?.workerFinished(token: token, result: result)
                } catch { await self?.workerFailed(token: token, message: error.localizedDescription) }
            }
            let deadline = Date.now.addingTimeInterval(35)
            while Date.now < deadline {
                try Task.checkCancellation()
                guard worker != nil else { throw EngineError.message(error ?? "The receiver could not start.") }
                if workerReady {
                    await refresh()
                    try Task.checkCancellation()
                    guard !shuttingDown else { throw CancellationError() }
                    if snapshot.workerAlive && (snapshot.connection == "connected" || !migrating) {
                        if migrating { try await client.action("commit-migration") }
                        return
                    }
                }
                try await Task.sleep(for: .milliseconds(400))
            }
            throw EngineError.message("The receiver did not become ready. Please retry.")
        } catch {
            await stopWorker()
            if migrating {
                let command = shuttingDown || Task.isCancelled ? "restore-stopped" : "restore"
                let client = client
                await Task.detached(priority: .utility) { try? await client.action(command) }.value
            }
            throw error
        }
    }

    private func receive(_ event: EngineEvent, token: UUID) {
        guard workerToken == token else { return }
        if event.event == "ready" { workerReady = true }
        if event.event == "error" { error = event.message ?? "The local receiver needs attention." }
        if event.event == "transcript", notifications, !preview, !isolated {
            let content = UNMutableNotificationContent()
            content.title = "Transcript ready"
            content.body = "A new WhatsApp voice note was transcribed on this Mac."
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: event.id ?? UUID().uuidString, content: content, trigger: nil))
        }
        if ["ready", "connection", "processing", "transcript"].contains(event.event ?? "") {
            Task { await refresh() }
        }
    }

    private func workerFinished(token: UUID, result: Int32) {
        guard workerToken == token else { return }
        worker = nil
        workerReady = false
        if !shuttingDown && !snapshot.paused {
            error = "The receiver stopped. Click Resume to restart it."
        }
        Task { await refresh() }
    }

    private func workerFailed(token: UUID, message: String) {
        guard workerToken == token else { return }
        worker = nil
        workerReady = false
        error = message
    }

    private func stopWorker() async {
        let process = worker
        worker = nil
        workerToken = nil
        workerReady = false
        if let process { await Task.detached(priority: .utility) { process.stop() }.value }
    }

    func linkPhone() {
        if preview { pairingCode = "DEMO-CODE"; return }
        perform { [self] in
            await stopWorker()
            try Task.checkCancellation()
            guard !shuttingDown else { throw CancellationError() }
            let process = EngineProcess(location: location, command: "pair")
            pairing = process
            let token = UUID()
            pairingToken = token
            let payload = try JSONEncoder().encode(["phone": phone])
            defer { pairing = nil; pairingToken = nil; phone = "" }
            var paired = false
            let box = PairingResult()
            let result = try await Task.detached(priority: .utility) { [weak self] in
                try process.stream(payload: payload) { [weak self] event in
                    if event.event == "paired" { box.setPaired() }
                    Task { @MainActor in
                        guard self?.pairingToken == token, self?.shuttingDown == false else { return }
                        if event.event == "pair_code" { self?.pairingCode = event.code }
                        if event.event == "error" { self?.error = event.message }
                    }
                }
            }.value
            try Task.checkCancellation()
            paired = box.paired
            pairingCode = nil
            guard result == 0 && paired else { throw EngineError.message(error ?? "WhatsApp did not finish linking. Please retry.") }
            await refresh()
            route = .inbox
            if !snapshot.paused && snapshot.modelReady { try await startCapture() }
        }
    }

    func cancelLink() {
        let process = pairing
        pairing = nil
        pairingToken = nil
        pairingCode = nil
        phone = ""
        operation?.cancel()
        if let process { Task.detached(priority: .utility) { process.stop() } }
    }

    func downloadModel() {
        if preview { downloadProgress = 1; snapshot.modelReady = true; return }
        perform { [self] in
            downloading = true
            downloadProgress = 0
            let directory = URL(fileURLWithPath: snapshot.modelPath).deletingLastPathComponent()
            let download = ModelDownload(directory: directory) { [weak self] progress in
                Task { @MainActor in self?.downloadProgress = progress }
            }
            downloader = download
            defer { downloading = false; downloader = nil }
            let part = try await download.download()
            defer { try? FileManager.default.removeItem(at: part) }
            try await Task.detached(priority: .utility) { try ModelVerification.verifyFile(part) }.value
            try Task.checkCancellation()
            try await client.action("verify-model", extra: ["--input", part.path])
            await refresh()
            if snapshot.authenticated && !snapshot.paused { try await startCapture(); route = .inbox }
        }
    }

    func cancelDownload() { downloader?.cancel(); operation?.cancel() }

    func setLogin(_ enabled: Bool) {
        guard !preview, !isolated else { launchAtLogin = enabled; return }
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            launchAtLogin = SMAppService.mainApp.status == .enabled
            if SMAppService.mainApp.status == .requiresApproval { SMAppService.openSystemSettingsLoginItems() }
        } catch { self.error = error.localizedDescription }
    }

    func setNotifications(_ enabled: Bool) {
        notifications = enabled
        guard !preview, !isolated else { return }
        UserDefaults.standard.set(enabled, forKey: "notifyWhenReady")
        if enabled {
            Task {
                let allowed = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])) ?? false
                if !allowed { notifications = false; UserDefaults.standard.set(false, forKey: "notifyWhenReady") }
            }
        }
    }

    func openFolder() {
        guard !snapshot.outputFolder.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: snapshot.outputFolder))
    }

    func copy(_ transcript: Transcript) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(transcript.text, forType: .string)
        notice = "Transcript copied."
    }

    func shutdown() async {
        shuttingDown = true
        monitor?.cancel()
        let starting = startup
        starting?.cancel()
        let pending = operation
        pending?.cancel()
        downloader?.cancel()
        let linking = pairing
        pairing = nil
        pairingToken = nil
        if let linking { await Task.detached(priority: .utility) { linking.stop() }.value }
        await client.cancelAll()
        await stopWorker()
        await pending?.value
        await starting?.value
    }
}

private final class PairingResult: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var paired: Bool { lock.withLock { value } }
    func setPaired() { lock.withLock { value = true } }
}
