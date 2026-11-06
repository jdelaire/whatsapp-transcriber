import Foundation
import Testing
@testable import TranscriberCore

actor WorkerStoreFixture: SummaryStoreClient {
    var settings = SummarySettings()
    var owner = ""
    var active: SummaryIdentity?
    var published: [[String]] = []
    var done = false
    var finishFailures = 0
    var finishAttempts = 0
    var scanCalls = 0
    var cachedPages = false

    func failNextFinishes(_ count: Int) { finishFailures = count }
    func simulateCachedPages() { done = true; cachedPages = true }

    func transcript(id: String) async throws -> TranscriptDetail { throw EngineError.message("unused") }
    func summaryStatus(id: String) async throws -> SummaryRecord { .init() }
    func summarySettings(enabled: Bool?) async throws -> SummarySettings {
        if let enabled, enabled != settings.enabled { settings.enabled = enabled; settings.revision += 1; active = nil }
        return settings
    }
    func summaryOwner(owner: String) async throws -> SummaryOwnerResult { self.owner = owner; active = nil; return .init(recovered: 0) }
    func summaryReconcile(owner: String, id: String?) async throws -> SummaryScanResult {
        scanCalls += 1
        return .init(visited: cachedPages ? 25 : 0, wrapped: !cachedPages)
    }
    func summaryClaim(owner: String) async throws -> SummaryClaimResult {
        guard settings.enabled, !done, active == nil else { return .init(claim: nil) }
        let identity = SummaryIdentity(owner: owner, id: "note", token: UUID().uuidString, sourceHash: "hash", promptVersion: 1, settingsRevision: settings.revision)
        active = identity
        return .init(claim: .init(identity: identity, text: "One short note", maxBullets: 1))
    }
    func summaryCheck(identity: SummaryIdentity) async throws -> SummaryCheckResult { .init(valid: active == identity && settings.enabled && identity.settingsRevision == settings.revision) }
    func summaryFinish(identity: SummaryIdentity, outcome: SummaryOutcome, bullets: [String], errorCode: String?) async throws -> SummaryMutationResult {
        finishAttempts += 1
        if finishFailures > 0 { finishFailures -= 1; throw EngineError.message("database is locked") }
        guard active == identity, settings.enabled, identity.settingsRevision == settings.revision else { return .init(accepted: false) }
        active = nil
        if outcome == .ready { published.append(bullets); done = true }
        return .init(accepted: true)
    }
    func summaryRetry(id: String) async throws -> SummaryMutationResult { .init(accepted: false) }
}

actor ControlledSummaryModel: SummaryModel {
    var available = true
    var started = 0
    var active = 0
    var maximumActive = 0
    var continuations: [CheckedContinuation<Void, Never>] = []

    func setAvailable(_ value: Bool) { available = value }
    func availability() async -> SummaryAvailability { available ? .available : .unavailable(.modelNotReady) }
    func languageCode(for body: String) async throws -> String? { "en" }
    func fits(_ request: SummaryModelRequest) async throws -> Bool { true }
    func respond(_ request: SummaryModelRequest) async throws -> [String] {
        started += 1; active += 1; maximumActive = max(maximumActive, active)
        await withCheckedContinuation { continuations.append($0) }
        active -= 1
        return ["A summary"]
    }
    func release() { if !continuations.isEmpty { continuations.removeFirst().resume() } }
}

private func eventually(_ condition: @escaping @Sendable () async -> Bool) async throws {
    for _ in 0..<300 {
        if await condition() { return }
        try await Task.sleep(for: .milliseconds(10))
    }
    throw EngineError.message("Timed out waiting for summary worker")
}

@Suite struct SummaryWorker {
    @Test func cachedLibraryDoesNotBusyLoopThroughPages() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = WorkerStoreFixture()
        await store.simulateCachedPages()
        let worker = TranscriberCore.SummaryWorker(store: store, model: ControlledSummaryModel(), dataRoot: root, onUpdate: { _ in })
        await worker.start()
        try await eventually { await store.scanCalls > 0 }
        try await Task.sleep(for: .milliseconds(50))
        #expect(await store.scanCalls == 1)
        await worker.stop()
    }

    @Test func failedInterruptionPersistenceRecoversWithoutRestart() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = WorkerStoreFixture()
        let model = ControlledSummaryModel()
        let worker = TranscriberCore.SummaryWorker(store: store, model: model, dataRoot: root, onUpdate: { _ in })
        await worker.start()
        try await eventually { await model.started == 1 }
        await store.failNextFinishes(2)
        await model.release()
        try await eventually { await store.finishAttempts >= 2 }
        try await eventually { await worker.wake(); return await model.started == 2 }
        await model.release()
        try await eventually { await store.published.count == 1 }
        await worker.stop()
    }

    @Test func offOnRejectsLateCompletionWithoutConcurrentRequests() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = WorkerStoreFixture()
        let model = ControlledSummaryModel()
        let worker = TranscriberCore.SummaryWorker(store: store, model: model, dataRoot: root, onUpdate: { _ in })
        await worker.start()
        try await eventually { await model.started == 1 }
        _ = try await store.summarySettings(enabled: false)
        await worker.settingsChanged()
        _ = try await store.summarySettings(enabled: true)
        await worker.settingsChanged()
        try await Task.sleep(for: .milliseconds(50))
        #expect(await model.started == 1)
        await model.release()
        try await eventually { await model.started == 2 }
        #expect(await store.published.isEmpty)
        await model.release()
        try await eventually { await store.published.count == 1 }
        #expect(await model.maximumActive == 1)
        await worker.stop()
    }

    @Test func uncooperativeModelCannotHangQuitOrReleaseOwnershipEarly() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = WorkerStoreFixture()
        let model = ControlledSummaryModel()
        let worker = TranscriberCore.SummaryWorker(store: store, model: model, dataRoot: root, onUpdate: { _ in })
        await worker.start()
        try await eventually { await model.started == 1 }
        let began = ContinuousClock.now
        await worker.stop(cleanupLimit: .milliseconds(100))
        #expect(ContinuousClock.now - began < .milliseconds(500))
        #expect(SummaryWorkerLock.acquire(dataRoot: root) == nil)
        await model.release()
        try await eventually {
            guard let lock = SummaryWorkerLock.acquire(dataRoot: root) else { return false }
            lock.release()
            return true
        }
        #expect(await store.published.isEmpty)
    }

    @Test func unavailableModelResumesOnWake() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = WorkerStoreFixture()
        let model = ControlledSummaryModel()
        await model.setAvailable(false)
        let worker = TranscriberCore.SummaryWorker(store: store, model: model, dataRoot: root, onUpdate: { _ in })
        await worker.start()
        try await Task.sleep(for: .milliseconds(50))
        #expect(await model.started == 0)
        await model.setAvailable(true)
        await worker.wake()
        try await eventually { await model.started == 1 }
        await model.release()
        try await eventually { await store.published.count == 1 }
        await worker.stop()
    }
}
