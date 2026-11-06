import Foundation

public enum SummaryState: String, Codable, Sendable {
    case pending, running, ready, failed

    public init(from decoder: any Decoder) throws {
        self = SummaryState(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .pending
    }
}

public struct SummaryRecord: Codable, Sendable, Equatable {
    public var state: SummaryState
    public var bullets: [String]
    public var errorCode: String?
    public var sourceHash: String?
    public var promptVersion: Int
    public var sourceFingerprint: String?

    public init(state: SummaryState = .pending, bullets: [String] = [], errorCode: String? = nil, sourceHash: String? = nil, promptVersion: Int = 1, sourceFingerprint: String? = nil) {
        self.state = state
        self.bullets = state == .ready ? bullets : []
        self.errorCode = errorCode
        self.sourceHash = sourceHash
        self.promptVersion = promptVersion
        self.sourceFingerprint = sourceFingerprint
    }

    private enum CodingKeys: String, CodingKey { case state, bullets, errorCode, sourceHash, promptVersion, sourceFingerprint }

    public init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        self.init(state: try values.decodeIfPresent(SummaryState.self, forKey: .state) ?? .pending,
                  bullets: try values.decodeIfPresent([String].self, forKey: .bullets) ?? [],
                  errorCode: try values.decodeIfPresent(String.self, forKey: .errorCode),
                  sourceHash: try values.decodeIfPresent(String.self, forKey: .sourceHash),
                  promptVersion: try values.decodeIfPresent(Int.self, forKey: .promptVersion) ?? 1,
                  sourceFingerprint: try values.decodeIfPresent(String.self, forKey: .sourceFingerprint))
    }

    public func statusMessage(enabled: Bool, availability: SummaryAvailability) -> String? {
        if state == .ready { return nil }
        if state == .failed {
            switch errorCode {
            case "original_missing": return String(localized: "Original transcript is missing.")
            case "original_unreadable": return String(localized: "Original transcript could not be read.")
            case "unsupported_language": return String(localized: "Apple Intelligence does not support this note's language.")
            case "refused": return String(localized: "Apple Intelligence could not summarize this note.")
            case "context_limit": return String(localized: "This note could not be summarized. Original text is available.")
            default: return String(localized: "Summary failed. You can retry.")
            }
        }
        if !enabled { return String(localized: "Automatic summaries paused.") }
        if case .unavailable(let reason) = availability { return reason.explanation }
        return state == .running ? String(localized: "Summarizing on this Mac…") : String(localized: "Summary queued.")
    }
}

public enum TranscriptSelection: Sendable, Equatable { case summary, original }

public struct TranscriptVisit: Sendable, Equatable {
    public private(set) var token = UUID()
    public private(set) var requestedID: String?
    public private(set) var detail: TranscriptDetail?
    public private(set) var selection: TranscriptSelection = .summary
    public private(set) var loading = false
    public private(set) var error: String?

    public init() {}

    @discardableResult public mutating func open(id: String) -> UUID {
        token = UUID(); requestedID = id; detail = nil; selection = .summary; loading = true; error = nil
        return token
    }

    public mutating func select(_ selection: TranscriptSelection) { self.selection = selection }

    @discardableResult public mutating func receive(_ document: TranscriptDetail, requestedID: String, token: UUID) -> Bool {
        guard self.token == token, self.requestedID == requestedID else { return false }
        detail = document; loading = false; error = nil
        return true
    }

    public mutating func fail(_ message: String, requestedID: String, token: UUID) {
        guard self.token == token, self.requestedID == requestedID else { return }
        loading = false; error = message
    }

    @discardableResult public mutating func update(_ summary: SummaryRecord, requestedID: String, token: UUID) -> Bool {
        guard self.token == token, self.requestedID == requestedID, detail != nil else { return false }
        let changed = summary.sourceFingerprint != nil && summary.sourceFingerprint != detail?.sourceFingerprint
        detail?.summary = summary
        if summary.errorCode == "original_missing" || summary.errorCode == "original_unreadable" { detail?.text = nil }
        return changed
    }

    public var showingSummary: Bool { selection == .summary && detail?.summary.state == .ready }
    public var displayedText: String? { showingSummary ? detail?.summary.bullets.map { "• \($0)" }.joined(separator: "\n\n") : detail?.text }
}

public struct SummarySettings: Codable, Sendable, Equatable {
    public var enabled: Bool
    public var revision: Int
    public var firstEnabledAt: Double?

    public init(enabled: Bool = true, revision: Int = 0, firstEnabledAt: Double? = nil) {
        self.enabled = enabled
        self.revision = revision
        self.firstEnabledAt = firstEnabledAt
    }
}

public struct TranscriptDetail: Codable, Sendable, Equatable {
    public let id: String
    public let title: String
    public let timestamp: String
    public let path: String
    public var text: String?
    public var sourceHash: String?
    public var sourceFingerprint: String?
    public var summary: SummaryRecord

    public init(id: String, title: String, timestamp: String, path: String, text: String?, sourceHash: String?, sourceFingerprint: String? = nil, summary: SummaryRecord = .init()) {
        self.id = id
        self.title = title
        self.timestamp = timestamp
        self.path = path
        self.text = text
        self.sourceHash = sourceHash
        self.sourceFingerprint = sourceFingerprint
        self.summary = summary
    }

    public var date: Date {
        (try? Date(timestamp, strategy: .iso8601)) ??
        (try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(timestamp)) ?? .distantPast
    }
}

public struct SummaryIdentity: Codable, Sendable, Equatable {
    public let owner: String
    public let id: String
    public let token: String
    public let sourceHash: String
    public let promptVersion: Int
    public let settingsRevision: Int
}

public struct SummaryClaim: Codable, Sendable, Equatable {
    public let identity: SummaryIdentity
    public let text: String
    public let maxBullets: Int
}

public struct SummaryScanResult: Codable, Sendable {
    public let visited: Int
    public let wrapped: Bool
    public let changed: Bool

    public init(visited: Int, wrapped: Bool, changed: Bool = false) { self.visited = visited; self.wrapped = wrapped; self.changed = changed }
    private enum CodingKeys: String, CodingKey { case visited, wrapped, changed }
    public init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        self.init(visited: try values.decode(Int.self, forKey: .visited), wrapped: try values.decode(Bool.self, forKey: .wrapped), changed: try values.decodeIfPresent(Bool.self, forKey: .changed) ?? false)
    }
}
public struct SummaryOwnerResult: Codable, Sendable { public let recovered: Int }
public struct SummaryClaimResult: Codable, Sendable { public let claim: SummaryClaim? }
public struct SummaryCheckResult: Codable, Sendable { public let valid: Bool }
public struct SummaryMutationResult: Codable, Sendable { public let accepted: Bool }
public enum SummaryOutcome: String, Codable, Sendable { case ready, failed, interrupted }

public protocol SummaryStoreClient: Sendable {
    func transcript(id: String) async throws -> TranscriptDetail
    func summaryStatus(id: String) async throws -> SummaryRecord
    func summarySettings(enabled: Bool?) async throws -> SummarySettings
    func summaryOwner(owner: String) async throws -> SummaryOwnerResult
    func summaryReconcile(owner: String, id: String?) async throws -> SummaryScanResult
    func summaryClaim(owner: String) async throws -> SummaryClaimResult
    func summaryCheck(identity: SummaryIdentity) async throws -> SummaryCheckResult
    func summaryFinish(identity: SummaryIdentity, outcome: SummaryOutcome, bullets: [String], errorCode: String?) async throws -> SummaryMutationResult
    func summaryRetry(id: String) async throws -> SummaryMutationResult
}

public enum SummaryUnavailableReason: String, Sendable {
    case requiresMacOS26 = "requires_macos_26"
    case deviceNotEligible = "device_not_eligible"
    case appleIntelligenceNotEnabled = "apple_intelligence_not_enabled"
    case modelNotReady = "model_not_ready"
    case unknown

    public var explanation: String {
        switch self {
        case .requiresMacOS26: String(localized: "Summaries require macOS 26 or later.")
        case .deviceNotEligible: String(localized: "Apple Intelligence is unavailable on this Mac.")
        case .appleIntelligenceNotEnabled: String(localized: "Enable Apple Intelligence in System Settings to create summaries.")
        case .modelNotReady: String(localized: "Apple Intelligence is preparing its model. Summaries will resume when ready.")
        case .unknown: String(localized: "Apple Intelligence is temporarily unavailable.")
        }
    }
}

public enum SummaryAvailability: Sendable, Equatable { case available, unavailable(SummaryUnavailableReason) }
public enum SummaryStage: Sendable, Equatable { case facts, final(maxBullets: Int) }
public struct SummaryModelRequest: Sendable {
    public let text: String
    public let languageCode: String?
    public let stage: SummaryStage
}

public enum SummaryGenerationError: Error, Sendable { case interrupted, unavailable(SummaryUnavailableReason), contextOverflow, note(code: String) }

public protocol SummaryModel: Sendable {
    func availability() async -> SummaryAvailability
    func languageCode(for body: String) async throws -> String?
    func fits(_ request: SummaryModelRequest) async throws -> Bool
    func respond(_ request: SummaryModelRequest) async throws -> [String]
}
