import Foundation
import Testing
@testable import TranscriberCore

actor FixtureSummaryModel: SummaryModel {
    enum Behavior: Sendable { case normal, noShrink, overflow, invalid, refused }
    let limit: Int
    let behavior: Behavior
    var requests: [SummaryModelRequest] = []
    var sourceSpans: [String] = []

    init(limit: Int = 4096, behavior: Behavior = .normal) { self.limit = limit; self.behavior = behavior }
    func availability() async -> SummaryAvailability { .available }
    func languageCode(for body: String) async throws -> String? { body.contains("Bonjour") ? "fr" : "en" }
    func fits(_ request: SummaryModelRequest) async throws -> Bool { request.text.utf8.count <= limit }
    func respond(_ request: SummaryModelRequest) async throws -> [String] {
        requests.append(request)
        if behavior == .refused { throw SummaryGenerationError.note(code: "refused") }
        if behavior == .invalid { return [""] }
        if behavior == .overflow && request.text.utf8.count > 80 { throw SummaryGenerationError.contextOverflow }
        switch request.stage {
        case .facts:
            sourceSpans.append(request.text)
            return behavior == .noShrink ? [request.text] : [request.text.contains("TAILFACT") ? "TAILFACT" : "Point"]
        case .final:
            return [request.text.contains("TAILFACT") ? "Résumé TAILFACT" : "Résumé"]
        }
    }
}

actor SummaryCheckpointCounter {
    var count = 0
    func check() throws {
        count += 1
        if count > 2 { throw SummaryGenerationError.interrupted }
    }
}

@Suite struct SummaryGeneration {
    @Test func longUnicodeInputReachesModelWithoutLosingSpansOrTail() async throws {
        let model = FixtureSummaryModel(limit: 600)
        let body = "Bonjour. " + String(repeating: "e\u{301} 👋 phrase.\n", count: 300) + "TAILFACT"
        let result = try await SummaryGenerator(model: model).generate(body: body, maxBullets: 3, checkpoint: {})
        #expect(result == ["Résumé TAILFACT"])
        #expect(await model.sourceSpans.joined() == body)
        #expect(await model.requests.allSatisfy { $0.languageCode == "fr" })
    }

    @Test func shortThresholdCountsUnicodeScalars() async throws {
        let model = FixtureSummaryModel()
        _ = try await SummaryGenerator(model: model).generate(body: String(repeating: "é", count: 280), maxBullets: 3, checkpoint: {})
        _ = try await SummaryGenerator(model: model).generate(body: String(repeating: "é", count: 281), maxBullets: 3, checkpoint: {})
        let stages = await model.requests.map(\.stage)
        #expect(stages == [.final(maxBullets: 1), .final(maxBullets: 3)])
    }

    @Test func inaccurateFitBudgetSplitsOverflowingSpans() async throws {
        let model = FixtureSummaryModel(behavior: .overflow)
        let body = String(repeating: "Long sentence without punctuation ", count: 30)
        _ = try await SummaryGenerator(model: model).generate(body: body, maxBullets: 3, checkpoint: {})
        let mapped = await model.sourceSpans.joined()
        #expect(String(mapped.prefix(body.count)) == body)
        #expect(await model.sourceSpans.allSatisfy { $0.utf8.count <= 80 })
    }

    @Test func expandingReductionFailsInsteadOfLooping() async {
        let model = FixtureSummaryModel(limit: 300, behavior: .noShrink)
        await #expect(throws: SummaryGenerationError.self) {
            try await SummaryGenerator(model: model).generate(body: String(repeating: "long ", count: 1000), maxBullets: 3, checkpoint: {})
        }
        #expect(await model.requests.count < 100)
    }

    @Test func interruptedOrInvalidGenerationNeverReturnsPartialSummary() async {
        let model = FixtureSummaryModel(limit: 300)
        let counter = SummaryCheckpointCounter()
        await #expect(throws: SummaryGenerationError.self) {
            try await SummaryGenerator(model: model).generate(body: String(repeating: "text ", count: 1000), maxBullets: 3, checkpoint: { try await counter.check() })
        }
        for behavior in [FixtureSummaryModel.Behavior.invalid, .refused] {
            await #expect(throws: SummaryGenerationError.self) {
                try await SummaryGenerator(model: FixtureSummaryModel(behavior: behavior)).generate(body: "Short", maxBullets: 1, checkpoint: {})
            }
        }
    }
}
