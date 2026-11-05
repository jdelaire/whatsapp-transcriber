import Foundation

public final class ModelDownload: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    private let progress: @Sendable (Double) -> Void
    private let directory: URL
    private let lock = NSLock()
    private var continuation: CheckedContinuation<URL, any Error>?
    private var session: URLSession?
    private var task: URLSessionDownloadTask?
    private var cancelled = false
    private var lastProgress = -1.0
    private var lastProgressAt = Date.distantPast

    public init(directory: URL, progress: @escaping @Sendable (Double) -> Void) {
        self.directory = directory
        self.progress = progress
    }

    public func download() async throws -> URL {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                lock.withLock {
                    guard !cancelled else { continuation.resume(throwing: CancellationError()); return }
                    self.continuation = continuation
                    let configuration = URLSessionConfiguration.ephemeral
                    configuration.timeoutIntervalForResource = 1800
                    let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
                    self.session = session
                    let task = session.downloadTask(with: URL(string: "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin")!)
                    self.task = task
                    task.resume()
                }
            }
        } onCancel: { self.cancel() }
    }

    public func cancel() {
        lock.withLock { cancelled = true; task?.cancel() }
    }

    public func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64, totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        let value = min(1, Double(totalBytesWritten) / Double(ModelVerification.bytes))
        let publish = lock.withLock { () -> Bool in
            guard value - lastProgress >= 0.005 || Date.now.timeIntervalSince(lastProgressAt) >= 0.2 || value == 1 else { return false }
            lastProgress = value
            lastProgressAt = .now
            return true
        }
        if publish { progress(value) }
    }

    public func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        do {
            guard let response = downloadTask.response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
                throw EngineError.message("The model download failed. Check your connection and retry.")
            }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let target = directory.appendingPathComponent("transcriber-model-\(UUID().uuidString).part")
            try FileManager.default.moveItem(at: location, to: target)
            finish(.success(target))
        } catch { finish(.failure(error)) }
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: (any Error)?) {
        if let error { finish(.failure(error)) }
    }

    private func finish(_ result: Result<URL, any Error>) {
        let pending = lock.withLock { () -> CheckedContinuation<URL, any Error>? in
            let value = continuation
            continuation = nil
            return value
        }
        pending?.resume(with: result)
        session?.finishTasksAndInvalidate()
    }
}
