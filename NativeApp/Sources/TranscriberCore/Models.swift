import Foundation
import CryptoKit

public enum CapturePhase: String, Sendable {
    case setup, checking, paused, stopped, reconnecting, connected, transcribing

    public var title: String {
        switch self {
        case .setup: "Set up Transcriber"
        case .checking: "Checking WhatsApp"
        case .paused: "Transcription paused"
        case .stopped: "Ready to start"
        case .reconnecting: "Connecting to WhatsApp"
        case .connected: "Connected to WhatsApp"
        case .transcribing: "Transcribing a voice note"
        }
    }
}

public struct Transcript: Codable, Identifiable, Sendable, Equatable {
    public let id: String
    public let title: String
    public let timestamp: String
    public let text: String
    public let path: String
    public var summary: SummaryRecord = .init()

    private enum CodingKeys: String, CodingKey { case id, title, timestamp, text, path, summary }

    public init(id: String, title: String, timestamp: String, text: String, path: String, summary: SummaryRecord = .init()) {
        self.id = id; self.title = title; self.timestamp = timestamp; self.text = text; self.path = path; self.summary = summary
    }

    public init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        self.init(id: try values.decode(String.self, forKey: .id), title: try values.decode(String.self, forKey: .title),
                  timestamp: try values.decode(String.self, forKey: .timestamp), text: try values.decode(String.self, forKey: .text),
                  path: try values.decode(String.self, forKey: .path), summary: try values.decodeIfPresent(SummaryRecord.self, forKey: .summary) ?? .init())
    }

    public var date: Date {
        (try? Date(timestamp, strategy: .iso8601)) ??
        (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(timestamp)) ?? .distantPast
    }
}

public struct EngineSnapshot: Codable, Sendable, Equatable {
    public var schemaVersion: Int
    public var configured: Bool
    public var authenticated: Bool
    public var authenticationError: String?
    public var paused: Bool
    public var modelReady: Bool
    public var workerAlive: Bool
    public var connection: String
    public var processing: Bool
    public var counts: [String: Int]
    public var recent: [Transcript]
    public var modelPath: String
    public var outputFolder: String
    public var migrationNeeded: Bool
    public var migrationPhase: String
    public var workerPid: Int?
    public var summarySettings = SummarySettings()
    public var summaryCounts: [String: Int] = [:]

    private enum CodingKeys: String, CodingKey {
        case schemaVersion, configured, authenticated, authenticationError, paused, modelReady, workerAlive, connection, processing, counts, recent, modelPath, outputFolder, migrationNeeded, migrationPhase, workerPid, summarySettings, summaryCounts
    }

    public init(from decoder: any Decoder) throws {
        self = .empty
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        configured = try values.decode(Bool.self, forKey: .configured)
        authenticated = try values.decode(Bool.self, forKey: .authenticated)
        authenticationError = try values.decodeIfPresent(String.self, forKey: .authenticationError)
        paused = try values.decode(Bool.self, forKey: .paused)
        modelReady = try values.decode(Bool.self, forKey: .modelReady)
        workerAlive = try values.decode(Bool.self, forKey: .workerAlive)
        connection = try values.decode(String.self, forKey: .connection)
        processing = try values.decode(Bool.self, forKey: .processing)
        counts = try values.decode([String: Int].self, forKey: .counts)
        recent = try values.decode([Transcript].self, forKey: .recent)
        modelPath = try values.decode(String.self, forKey: .modelPath)
        outputFolder = try values.decode(String.self, forKey: .outputFolder)
        migrationNeeded = try values.decode(Bool.self, forKey: .migrationNeeded)
        migrationPhase = try values.decode(String.self, forKey: .migrationPhase)
        workerPid = try values.decodeIfPresent(Int.self, forKey: .workerPid)
        summarySettings = try values.decodeIfPresent(SummarySettings.self, forKey: .summarySettings) ?? .init()
        summaryCounts = try values.decodeIfPresent([String: Int].self, forKey: .summaryCounts) ?? [:]
    }

    private init(schemaVersion: Int, configured: Bool, authenticated: Bool, paused: Bool, modelReady: Bool, workerAlive: Bool, connection: String, processing: Bool, counts: [String: Int], recent: [Transcript], modelPath: String, outputFolder: String, migrationNeeded: Bool, migrationPhase: String) {
        self.schemaVersion = schemaVersion; self.configured = configured; self.authenticated = authenticated
        self.paused = paused; self.modelReady = modelReady; self.workerAlive = workerAlive; self.connection = connection
        self.processing = processing; self.counts = counts; self.recent = recent; self.modelPath = modelPath
        self.outputFolder = outputFolder; self.migrationNeeded = migrationNeeded; self.migrationPhase = migrationPhase
    }

    public var phase: CapturePhase {
        if !configured || !modelReady { return .setup }
        if paused { return .paused }
        if authenticationError != nil { return .checking }
        if !authenticated { return .setup }
        if !workerAlive { return .stopped }
        if processing { return .transcribing }
        return connection == "connected" ? .connected : .reconnecting
    }

    public static let empty = EngineSnapshot(schemaVersion: 1, configured: false, authenticated: false,
        paused: false, modelReady: false, workerAlive: false, connection: "stopped", processing: false,
        counts: [:], recent: [], modelPath: "", outputFolder: "", migrationNeeded: false, migrationPhase: "none")

    public static var preview: EngineSnapshot {
        var state = empty
        state.configured = true
        state.authenticated = true
        state.modelReady = true
        state.workerAlive = true
        state.connection = "connected"
        state.recent = [
            Transcript(id: "sample-french", title: "Camille", timestamp: Date.now.addingTimeInterval(-120).ISO8601Format(), text: "Je serai là vers dix-huit heures. On se retrouve devant le café ?", path: ""),
            Transcript(id: "sample-english", title: "Alex", timestamp: Date.now.addingTimeInterval(-1080).ISO8601Format(), text: "The meeting is tomorrow at ten. I'll send you the address this afternoon.", path: ""),
            Transcript(id: "sample-weekend", title: "Weekend plans", timestamp: Date.now.addingTimeInterval(-2520).ISO8601Format(), text: "I've booked a table for four. See you at seven!", path: "")
        ]
        state.recent[0].summary = .init(state: .ready, bullets: ["Camille arrive vers 18 h et propose de se retrouver devant le café."], sourceHash: "preview-french", sourceFingerprint: "preview-french")
        state.recent[1].summary = .init(state: .ready, bullets: ["Meeting tomorrow at 10; Alex will send the address this afternoon."], sourceHash: "preview-english", sourceFingerprint: "preview-english")
        state.recent[2].summary = .init(state: .ready, bullets: ["Table booked for four at 7."], sourceHash: "preview-weekend", sourceFingerprint: "preview-weekend")
        state.summaryCounts = ["total": 3, "ready": 3]
        return state
    }
}

public struct EngineEvent: Decodable, Sendable {
    public let event: String?
    public let code: String?
    public let message: String?
    public let state: String?
    public let id: String?
    public let active: Bool?
    public let group: Int32?
}

public struct EventDecoder: Sendable {
    private var buffer = Data()
    public init() {}

    public mutating func append(_ chunk: Data) throws -> [EngineEvent] {
        buffer.append(chunk)
        guard buffer.count < 1_048_576 else { throw EngineError.message("The engine returned too much data.") }
        var events: [EngineEvent] = []
        while let end = buffer.firstIndex(of: 10) {
            let line = buffer[..<end]
            buffer.removeSubrange(...end)
            if !line.isEmpty { events.append(try JSONDecoder().decode(EngineEvent.self, from: line)) }
        }
        return events
    }
}

public enum EngineError: Error, LocalizedError, Sendable {
    case message(String)
    public var errorDescription: String? {
        switch self { case .message(let text): text }
    }
}

public enum ModelVerification {
    public static let bytes: Int64 = 487601967
    public static let sha256 = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"
    public static func matches(_ data: Data, bytes: Int, sha256: String) -> Bool {
        data.count == bytes && SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined() == sha256
    }
    public static func verifyFile(_ url: URL) throws {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        var count: Int64 = 0
        while let chunk = try handle.read(upToCount: 1_048_576), !chunk.isEmpty {
            count += Int64(chunk.count)
            hasher.update(data: chunk)
        }
        let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        guard count == bytes && digest == sha256 else {
            throw EngineError.message("The downloaded speech model failed verification. Please retry.")
        }
    }
}
