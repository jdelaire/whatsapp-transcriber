import Foundation

public struct SummaryGenerator: Sendable {
    private let model: any SummaryModel

    public init(model: any SummaryModel) { self.model = model }

    public func generate(body: String, maxBullets: Int, checkpoint: @escaping @Sendable () async throws -> Void) async throws -> [String] {
        guard !body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw SummaryGenerationError.note(code: "invalid_output") }
        let language = try await model.languageCode(for: body)
        let count = body.unicodeScalars.count <= 280 ? 1 : min(3, maxBullets)
        guard count > 0 else { throw SummaryGenerationError.note(code: "invalid_output") }
        let direct = SummaryModelRequest(text: body, languageCode: language, stage: .final(maxBullets: count))
        if try await model.fits(direct) {
            do { return try await response(direct, checkpoint: checkpoint) }
            catch SummaryGenerationError.contextOverflow {}
        }
        var facts: [String] = []
        for chunk in split(body, maximumBytes: 2048) {
            facts += try await extract(chunk, language: language, depth: 0, checkpoint: checkpoint)
        }
        var text = facts.joined(separator: "\n")
        for _ in 0..<16 {
            let request = SummaryModelRequest(text: text, languageCode: language, stage: .final(maxBullets: count))
            if try await model.fits(request) {
                do { return try await response(request, checkpoint: checkpoint) }
                catch SummaryGenerationError.contextOverflow {}
            }
            var reduced: [String] = []
            for group in split(text, maximumBytes: min(2048, max(1, text.utf8.count / 2))) {
                reduced += try await extract(group, language: language, depth: 0, checkpoint: checkpoint)
            }
            let next = reduced.joined(separator: "\n")
            guard next.utf8.count < text.utf8.count else { throw SummaryGenerationError.note(code: "context_limit") }
            text = next
        }
        throw SummaryGenerationError.note(code: "context_limit")
    }

    private func extract(_ text: String, language: String?, depth: Int, checkpoint: @escaping @Sendable () async throws -> Void) async throws -> [String] {
        let request = SummaryModelRequest(text: text, languageCode: language, stage: .facts)
        if try await model.fits(request) {
            do { return try await response(request, checkpoint: checkpoint) }
            catch SummaryGenerationError.contextOverflow {}
        }
        guard depth < 12, text.unicodeScalars.count > 1 else { throw SummaryGenerationError.note(code: "context_limit") }
        let parts = split(text, maximumBytes: max(1, text.utf8.count / 2))
        guard parts.count > 1 else { throw SummaryGenerationError.note(code: "context_limit") }
        var result: [String] = []
        for part in parts { result += try await extract(part, language: language, depth: depth + 1, checkpoint: checkpoint) }
        return result
    }

    private func response(_ request: SummaryModelRequest, checkpoint: @escaping @Sendable () async throws -> Void) async throws -> [String] {
        try Task.checkCancellation()
        try await checkpoint()
        let result = try await model.respond(request)
        try Task.checkCancellation()
        let maximum: Int
        switch request.stage { case .facts: maximum = 8; case .final(let count): maximum = count }
        guard (1...maximum).contains(result.count), result.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            throw SummaryGenerationError.note(code: "invalid_output")
        }
        return result
    }

    private func split(_ text: String, maximumBytes: Int) -> [String] {
        let scalars = text.unicodeScalars
        var start = scalars.startIndex
        var result: [String] = []
        while start < scalars.endIndex {
            var end = start
            var bytes = 0
            var boundary: String.UnicodeScalarView.Index?
            while end < scalars.endIndex {
                let scalar = scalars[end]
                let size = scalar.utf8.count
                if bytes + size > maximumBytes, end > start { break }
                bytes += size
                end = scalars.index(after: end)
                if bytes >= maximumBytes / 2 && (scalar == "." || scalar == "!" || scalar == "?" || scalar == "\n") { boundary = end }
            }
            if end < scalars.endIndex, let boundary { end = boundary }
            result.append(String(scalars[start..<end]))
            start = end
        }
        return result
    }
}
