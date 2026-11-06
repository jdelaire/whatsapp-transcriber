import Foundation
import Darwin

public struct EngineLocation: Sendable {
    public let executable: URL
    public let tools: URL
    public let dataRoot: URL?

    public init(bundle: URL, dataRoot: URL? = nil) {
        executable = bundle.appendingPathComponent("Contents/Helpers/Engine.app/Contents/MacOS/WAEngine")
        tools = bundle.appendingPathComponent("Contents/Helpers/Tools")
        self.dataRoot = dataRoot
    }

    public func arguments(_ command: String, extra: [String] = []) -> [String] {
        [command, "--tools", tools.path] + (dataRoot.map { ["--data-root", $0.path] } ?? []) + extra
    }
}

final class ProcessData: @unchecked Sendable {
    private let lock = NSLock()
    private var value = Data()
    func set(_ data: Data) { lock.withLock { value = data } }
    func get() -> Data { lock.withLock { value } }
}

public final class EngineProcess: @unchecked Sendable {
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private let errors = Pipe()
    private let lock = NSLock()
    private let shutdownLock = NSLock()
    private let terminated = DispatchGroup()
    private let errorsFinished = DispatchGroup()
    private let supervised: Bool
    private var cancelled = false
    private var started = false
    private var groups: Set<Int32> = []

    public init(location: EngineLocation, command: String, extra: [String] = []) {
        supervised = ["run", "pair", "migrate"].contains(command)
        process.executableURL = location.executable
        process.arguments = location.arguments(command, extra: extra)
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": NSHomeDirectory(), "PYTHONUNBUFFERED": "1"]
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        let terminated = self.terminated
        process.terminationHandler = { _ in terminated.leave() }
    }

    private func start(payload: Data? = nil) throws {
        try lock.withLock {
            guard !cancelled else { throw CancellationError() }
            terminated.enter()
            do { try process.run() } catch { terminated.leave(); throw error }
            started = true
        }
        if let payload {
            try input.fileHandleForWriting.write(contentsOf: payload)
            try input.fileHandleForWriting.write(contentsOf: Data([10]))
        }
    }

    public func collect(payload: Data? = nil, timeout: TimeInterval = 30) throws -> Data {
        defer { stop() }
        let deadline = Date.now.addingTimeInterval(timeout)
        try start()
        let group = DispatchGroup()
        let stdout = ProcessData()
        group.enter()
        DispatchQueue.global(qos: .utility).async { [self] in
            stdout.set((try? output.fileHandleForReading.readToEnd()) ?? Data())
            group.leave()
        }
        group.enter()
        errorsFinished.enter()
        DispatchQueue.global(qos: .utility).async { [self] in
            consumeErrors()
            errorsFinished.leave()
            group.leave()
        }
        let writing = ProcessData()
        if let payload {
            let descriptor = lock.withLock { cancelled ? -1 : dup(input.fileHandleForWriting.fileDescriptor) }
            guard descriptor >= 0 else { throw CancellationError() }
            _ = fcntl(descriptor, F_SETFL, fcntl(descriptor, F_GETFL) | O_NONBLOCK)
            _ = fcntl(descriptor, F_SETNOSIGPIPE, 1)
            group.enter()
            DispatchQueue.global(qos: .utility).async { [self] in
                defer {
                    close(descriptor)
                    try? input.fileHandleForWriting.close()
                    group.leave()
                }
                payload.withUnsafeBytes { bytes in
                    var offset = 0
                    while offset < bytes.count {
                        if Date.now >= deadline || lock.withLock({ cancelled }) { return }
                        let count = write(descriptor, bytes.baseAddress!.advanced(by: offset), min(16384, bytes.count - offset))
                        if count > 0 { offset += count }
                        else if errno == EAGAIN || errno == EINTR { Thread.sleep(forTimeInterval: 0.002) }
                        else { writing.set(Data([1])); return }
                    }
                }
            }
        }
        if terminated.wait(timeout: .now() + max(0, deadline.timeIntervalSinceNow)) == .timedOut {
            stop()
            throw EngineError.message("The local engine took too long. Please retry.")
        }
        if group.wait(timeout: .now() + max(0, deadline.timeIntervalSinceNow)) == .timedOut {
            throw EngineError.message("The local engine took too long. Please retry.")
        }
        if !writing.get().isEmpty { throw EngineError.message("The local engine could not receive the request.") }
        let data = stdout.get()
        if process.terminationStatus != 0 && data.isEmpty {
            throw EngineError.message("The local engine could not start. Reinstall the app if this continues.")
        }
        return data
    }

    public func stream(payload: Data? = nil, event: @escaping @Sendable (EngineEvent) -> Void) throws -> Int32 {
        defer { stop() }
        try start(payload: payload)
        let errorGroup = DispatchGroup()
        errorGroup.enter()
        errorsFinished.enter()
        DispatchQueue.global(qos: .utility).async { [self] in
            consumeErrors()
            errorsFinished.leave()
            errorGroup.leave()
        }
        var decoder = EventDecoder()
        while let chunk = try readChunk(output.fileHandleForReading) {
            for value in try decoder.append(chunk) { event(value) }
        }
        process.waitUntilExit()
        errorGroup.wait()
        return process.terminationStatus
    }

    private func readChunk(_ handle: FileHandle) throws -> Data? {
        var bytes = [UInt8](repeating: 0, count: 8192)
        while true {
            let count = Darwin.read(handle.fileDescriptor, &bytes, bytes.count)
            if count > 0 { return Data(bytes.prefix(count)) }
            if count == 0 { return nil }
            if errno != EINTR { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        }
    }

    private func consumeErrors() {
        var pending = Data()
        while let chunk = try? readChunk(errors.fileHandleForReading) {
            pending.append(chunk)
            while let newline = pending.firstIndex(of: 10) {
                let line = pending.prefix(upTo: newline)
                pending.removeSubrange(...newline)
                if let value = try? JSONDecoder().decode(EngineEvent.self, from: line), let group = value.group, group > 1 {
                    lock.withLock {
                        if value.event == "child_started" { groups.insert(group) }
                        if value.event == "child_stopped" { groups.remove(group) }
                    }
                }
            }
            if pending.count > 65536 { pending.removeAll() }
        }
    }

    public func stop() {
        shutdownLock.lock()
        defer { shutdownLock.unlock() }
        let live = lock.withLock { () -> Bool in
            cancelled = true
            try? input.fileHandleForWriting.close()
            return started && process.isRunning
        }
        if live {
            if !supervised && process.isRunning { process.terminate() }
            if terminated.wait(timeout: .now() + 10) == .timedOut {
                if process.isRunning { process.terminate() }
                if terminated.wait(timeout: .now() + 8) == .timedOut, process.isRunning {
                    kill(process.processIdentifier, SIGKILL)
                    process.waitUntilExit()
                }
            }
        }
        _ = errorsFinished.wait(timeout: .now() + 1)
        let owned = lock.withLock { groups }
        for group in owned { kill(-group, SIGTERM) }
        if !owned.isEmpty {
            let deadline = Date.now.addingTimeInterval(1)
            while Date.now < deadline && owned.contains(where: { kill(-$0, 0) == 0 }) { Thread.sleep(forTimeInterval: 0.02) }
            for group in owned where kill(-group, 0) == 0 { kill(-group, SIGKILL) }
        }
    }
}

private struct Envelope<Value: Decodable>: Decodable {
    let ok: Bool?
    let data: Value?
    let message: String?
}

private struct Acknowledgement: Decodable, Sendable {}

public actor EngineClient: SummaryStoreClient {
    public let location: EngineLocation
    private var active: [UUID: EngineProcess] = [:]

    public init(location: EngineLocation) { self.location = location }

    private func request<Value: Decodable & Sendable>(_ command: String, extra: [String] = [], payload: Data? = nil, type: Value.Type) async throws -> Value {
        let id = UUID()
        let process = EngineProcess(location: location, command: command, extra: extra)
        active[id] = process
        defer { active[id] = nil }
        let data = try await withTaskCancellationHandler {
            try await Task.detached(priority: .utility) { try process.collect(payload: payload, timeout: command == "migrate" ? 60 : 30) }.value
        } onCancel: {
            DispatchQueue.global(qos: .utility).async { process.stop() }
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(Envelope<Value>.self, from: data)
        guard response.ok == true, let value = response.data else {
            throw EngineError.message(response.message ?? "The local engine could not complete this operation.")
        }
        return value
    }

    public func snapshot(command: String = "status") async throws -> EngineSnapshot {
        try await request(command, type: EngineSnapshot.self)
    }

    public func action(_ command: String, extra: [String] = []) async throws {
        let _: Acknowledgement = try await request(command, extra: extra, type: Acknowledgement.self)
    }

    private func summaryRequest<Value: Decodable & Sendable>(_ command: String, payload: SummaryRequest, type: Value.Type) async throws -> Value {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return try await request(command, payload: encoder.encode(payload), type: type)
    }

    public func transcript(id: String) async throws -> TranscriptDetail { try await summaryRequest("transcript", payload: .init(id: id), type: TranscriptDetail.self) }
    public func summaryStatus(id: String) async throws -> SummaryRecord { try await summaryRequest("summary-status", payload: .init(id: id), type: SummaryRecord.self) }
    public func summarySettings(enabled: Bool? = nil) async throws -> SummarySettings { try await summaryRequest("summary-settings", payload: .init(enabled: enabled), type: SummarySettings.self) }
    public func summaryOwner(owner: String) async throws -> SummaryOwnerResult { try await summaryRequest("summary-owner", payload: .init(owner: owner), type: SummaryOwnerResult.self) }
    public func summaryReconcile(owner: String, id: String? = nil) async throws -> SummaryScanResult { try await summaryRequest("summary-reconcile", payload: .init(owner: owner, id: id), type: SummaryScanResult.self) }
    public func summaryClaim(owner: String) async throws -> SummaryClaimResult { try await summaryRequest("summary-claim", payload: .init(owner: owner), type: SummaryClaimResult.self) }
    public func summaryCheck(identity: SummaryIdentity) async throws -> SummaryCheckResult { try await summaryRequest("summary-check", payload: .init(identity: identity), type: SummaryCheckResult.self) }
    public func summaryFinish(identity: SummaryIdentity, outcome: SummaryOutcome, bullets: [String], errorCode: String?) async throws -> SummaryMutationResult { try await summaryRequest("summary-finish", payload: .init(identity: identity, outcome: outcome, bullets: bullets, errorCode: errorCode), type: SummaryMutationResult.self) }
    public func summaryRetry(id: String) async throws -> SummaryMutationResult { try await summaryRequest("summary-retry", payload: .init(id: id), type: SummaryMutationResult.self) }

    public func cancelAll() async {
        let processes = Array(active.values)
        await withTaskGroup(of: Void.self) { group in
            for process in processes { group.addTask { process.stop() } }
        }
    }
}

private struct SummaryRequest: Encodable, Sendable {
    var owner: String? = nil
    var id: String? = nil
    var enabled: Bool? = nil
    var identity: SummaryIdentity? = nil
    var outcome: SummaryOutcome? = nil
    var bullets: [String]? = nil
    var errorCode: String? = nil
}
