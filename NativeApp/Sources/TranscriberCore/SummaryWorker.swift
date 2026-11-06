import CryptoKit
import Darwin
import Foundation

public final class SummaryWorkerLock: @unchecked Sendable {
    private let mutex = NSLock()
    private var descriptor: Int32

    private init(descriptor: Int32) { self.descriptor = descriptor }

    public static func acquire(dataRoot: URL) -> SummaryWorkerLock? {
        let path = dataRoot.appendingPathComponent("summary-worker.lock").path
        let descriptor = open(path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
        guard descriptor >= 0 else { return nil }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else { close(descriptor); return nil }
        return SummaryWorkerLock(descriptor: descriptor)
    }

    public func release() {
        mutex.withLock {
            if descriptor >= 0 { flock(descriptor, LOCK_UN); close(descriptor); descriptor = -1 }
        }
    }

    deinit { release() }
}

public enum SummaryWorkerUpdate: Sendable {
    case availability(SummaryAvailability)
    case completed(id: String)
}

public actor SummaryWorker {
    private let store: any SummaryStoreClient
    private let model: any SummaryModel
    private let dataRoot: URL
    private let onUpdate: @Sendable (SummaryWorkerUpdate) async -> Void
    private let owner = UUID().uuidString
    private var ownership: SummaryWorkerLock?
    private var loop: Task<Void, Never>?
    private var polling: Task<Void, Never>?
    private var idle: Task<Void, Never>?
    private var generation: Task<[String], any Error>?
    private var identity: SummaryIdentity?
    private var observer: (any NSObjectProtocol)?
    private var hints: [String] = []
    private var stopped = false
    private var epoch = 0
    private var revision: Int?
    private var available: SummaryAvailability = .unavailable(.unknown)
    private var cleanupFinished = false

    public init(store: any SummaryStoreClient, model: any SummaryModel, dataRoot: URL, onUpdate: @escaping @Sendable (SummaryWorkerUpdate) async -> Void) {
        self.store = store
        self.model = model
        self.dataRoot = dataRoot
        self.onUpdate = onUpdate
    }

    private static func notificationName(dataRoot: URL) -> Notification.Name {
        let bytes = Data(dataRoot.resolvingSymlinksInPath().standardizedFileURL.path.utf8)
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        return .init("com.jdelaire.transcriber.summaries.\(digest)")
    }

    public nonisolated static func notifySettingsChanged(dataRoot: URL) {
        DistributedNotificationCenter.default().postNotificationName(notificationName(dataRoot: dataRoot), object: nil, userInfo: nil, deliverImmediately: true)
    }

    public func start() {
        guard loop == nil, !stopped else { return }
        observer = DistributedNotificationCenter.default().addObserver(forName: Self.notificationName(dataRoot: dataRoot), object: nil, queue: nil) { [weak self] _ in
            Task { await self?.settingsChanged() }
        }
        loop = Task(priority: .utility) { [weak self] in await self?.run() }
    }

    public func wake(transcriptID: String? = nil) {
        if let transcriptID, !hints.contains(transcriptID), hints.count < 100 { hints.append(transcriptID) }
        idle?.cancel()
    }

    public func settingsChanged() async {
        guard !stopped else { return }
        if let settings = try? await store.summarySettings(enabled: nil) {
            if let identity, !settings.enabled || identity.settingsRevision != settings.revision {
                epoch += 1
                generation?.cancel()
            }
            revision = settings.revision
        }
        idle?.cancel()
    }

    @discardableResult public func refreshAvailability() async -> SummaryAvailability {
        let current = await model.availability()
        if current != available {
            available = current
            await onUpdate(.availability(current))
        }
        if case .unavailable = current, generation != nil { epoch += 1; generation?.cancel() }
        return current
    }

    public func stop(cleanupLimit: Duration = .seconds(2)) async {
        guard !stopped else { return }
        stopped = true
        epoch += 1
        idle?.cancel()
        polling?.cancel()
        generation?.cancel()
        loop?.cancel()
        if let observer { DistributedNotificationCenter.default().removeObserver(observer); self.observer = nil }
        let claim = identity
        let store = store
        Task {
            if let claim { _ = try? await store.summaryFinish(identity: claim, outcome: .interrupted, bullets: [], errorCode: nil) }
            cleanupDidFinish()
        }
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: cleanupLimit)
        while !cleanupFinished && clock.now < deadline { try? await Task.sleep(for: .milliseconds(10)) }
    }

    private func cleanupDidFinish() { cleanupFinished = true }

    private func pause() async {
        guard !stopped else { return }
        let task = Task<Void, Never> { try? await Task.sleep(for: .seconds(20)) }
        idle = task
        await task.value
        idle = nil
    }

    private func checkpoint(_ claim: SummaryIdentity, epoch expected: Int) async throws {
        guard !stopped, epoch == expected, !Task.isCancelled else { throw SummaryGenerationError.interrupted }
        guard try await store.summaryCheck(identity: claim).valid else { throw SummaryGenerationError.interrupted }
        if case .unavailable(let reason) = await refreshAvailability() { throw SummaryGenerationError.unavailable(reason) }
        guard !stopped, epoch == expected, !Task.isCancelled else { throw SummaryGenerationError.interrupted }
    }

    private func watchSettings() async {
        while !stopped && !Task.isCancelled {
            await settingsChanged()
            await refreshAvailability()
            do { try await Task.sleep(for: .seconds(1)) } catch { return }
        }
    }

    private func run() async {
        defer { ownership?.release(); ownership = nil }
        while !stopped && !Task.isCancelled {
            await refreshAvailability()
            if ownership == nil {
                guard let acquired = SummaryWorkerLock.acquire(dataRoot: dataRoot) else { await pause(); continue }
                ownership = acquired
                do { _ = try await store.summaryOwner(owner: owner) }
                catch { ownership?.release(); ownership = nil; await pause(); continue }
            }
            guard !stopped else { return }
            do {
                if let unresolved = identity, generation == nil {
                    _ = try await store.summaryFinish(identity: unresolved, outcome: .interrupted, bullets: [], errorCode: nil)
                    identity = nil
                }
                let settings = try await store.summarySettings(enabled: nil)
                revision = settings.revision
                guard settings.enabled, available == .available else { await pause(); continue }
                let hint = hints.isEmpty ? nil : hints.removeFirst()
                let scan = try await store.summaryReconcile(owner: owner, id: hint)
                guard !stopped else { return }
                guard let claim = try await store.summaryClaim(owner: owner).claim else {
                    if !scan.wrapped && scan.changed { continue }
                    await pause()
                    continue
                }
                if stopped {
                    _ = try? await store.summaryFinish(identity: claim.identity, outcome: .interrupted, bullets: [], errorCode: nil)
                    return
                }
                identity = claim.identity
                let expectedEpoch = epoch
                let generator = SummaryGenerator(model: model)
                let task = Task(priority: .utility) { [weak self] in
                    try await generator.generate(body: claim.text, maxBullets: claim.maxBullets) { [weak self] in
                        guard let self else { throw SummaryGenerationError.interrupted }
                        try await self.checkpoint(claim.identity, epoch: expectedEpoch)
                    }
                }
                generation = task
                polling = Task { [weak self] in await self?.watchSettings() }
                var delay = false
                var unresolved = false
                do {
                    let bullets = try await task.value
                    try await checkpoint(claim.identity, epoch: expectedEpoch)
                    let result = try await store.summaryFinish(identity: claim.identity, outcome: .ready, bullets: bullets, errorCode: nil)
                    unresolved = !result.accepted
                    if result.accepted { await onUpdate(.completed(id: claim.identity.id)) }
                } catch {
                    let outcome: SummaryOutcome
                    let code: String?
                    if case SummaryGenerationError.note(let errorCode) = error { outcome = .failed; code = errorCode }
                    else { outcome = .interrupted; code = nil; delay = !(error is CancellationError) }
                    let result = try? await store.summaryFinish(identity: claim.identity, outcome: outcome, bullets: [], errorCode: code)
                    unresolved = result?.accepted != true
                    if result?.accepted == true && outcome == .failed { await onUpdate(.completed(id: claim.identity.id)) }
                }
                polling?.cancel()
                polling = nil
                generation = nil
                if !unresolved { identity = nil }
                if delay && !stopped { await pause() }
            } catch {
                await pause()
            }
        }
    }
}
