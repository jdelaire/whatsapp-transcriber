import Foundation
import Testing
@testable import TranscriberCore

@Suite struct SummaryContract {
    private func decode<Value: Decodable>(_ type: Value.Type, _ json: String) throws -> Value {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: Data(json.utf8))
    }

    @Test func oldSnapshotsKeepTranscriptsUsable() throws {
        let json = """
        {"schema_version":1,"configured":true,"authenticated":true,"paused":false,"model_ready":true,"worker_alive":true,"connection":"connected","processing":false,"counts":{},"recent":[{"id":"a","title":"Example","timestamp":"2026-01-01T00:00:00Z","text":"Original","path":"/tmp/a.txt"}],"model_path":"","output_folder":"","migration_needed":false,"migration_phase":"none"}
        """
        let snapshot = try decode(EngineSnapshot.self, json)
        #expect(snapshot.summarySettings.enabled)
        #expect(snapshot.recent[0].summary.state == .pending)
        #expect(snapshot.recent[0].text == "Original")
    }

    @Test func unknownOrPendingStatesNeverExposeBullets() throws {
        let unknown = try decode(SummaryRecord.self, """
        {"state":"future_state","bullets":["stale"],"error_code":null,"source_hash":null,"prompt_version":1}
        """)
        #expect(unknown.state == .pending)
        #expect(unknown.bullets.isEmpty)
        let pending = try decode(SummaryRecord.self, """
        {"state":"pending","bullets":["stale"],"error_code":null,"source_hash":"hash","prompt_version":1}
        """)
        #expect(pending.bullets.isEmpty)
    }

    @Test func detailAndClaimDecodeEngineWireValues() throws {
        let detail = try decode(TranscriptDetail.self, """
        {"id":"a","title":"Example","timestamp":"2026-01-01T00:00:00Z","path":"/tmp/a.txt","text":null,"source_hash":null,"summary":{"state":"failed","bullets":[],"error_code":"original_missing","source_hash":null,"prompt_version":1}}
        """)
        #expect(detail.text == nil)
        #expect(detail.summary.errorCode == "original_missing")
        let result = try decode(SummaryClaimResult.self, """
        {"claim":{"identity":{"owner":"o","id":"a","token":"t","source_hash":"h","prompt_version":1,"settings_revision":2},"text":"Bonjour","max_bullets":1}}
        """)
        #expect(result.claim?.identity.settingsRevision == 2)
        #expect(result.claim?.maxBullets == 1)
        #expect(try decode(SummaryClaimResult.self, "{\"claim\":null}").claim == nil)
    }
}

@Test func largeSummaryPayloadRoundTrip() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let executable = root.appendingPathComponent("Contents/Helpers/Engine.app/Contents/MacOS/WAEngine")
    try FileManager.default.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
    let script = "#!/usr/bin/python3\nimport sys,json\nsys.stdout.write(' ' * 1100000)\nsys.stdout.flush()\ntext=sys.stdin.buffer.read().decode()\nprint(json.dumps({'text':text}))\n"
    try script.write(to: executable, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
    let process = EngineProcess(location: EngineLocation(bundle: root), command: "transcript")
    defer { process.stop() }
    let body = String(repeating: "Bonjour 👋\n", count: 100000)
    let result = try process.collect(payload: Data(body.utf8), timeout: 5)
    let decoded = try JSONDecoder().decode([String: String].self, from: result)
    #expect(decoded["text"] == body)
}

@Test func summaryInputTimeoutIncludesBlockedWriter() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let executable = root.appendingPathComponent("Contents/Helpers/Engine.app/Contents/MacOS/WAEngine")
    try FileManager.default.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
    try "#!/bin/sh\nexec /bin/sleep 60\n".write(to: executable, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
    let process = EngineProcess(location: EngineLocation(bundle: root), command: "transcript")
    defer { process.stop() }
    let began = Date.now
    #expect(throws: (any Error).self) { try process.collect(payload: Data(repeating: 65, count: 2000000), timeout: 0.2) }
    #expect(Date.now.timeIntervalSince(began) < 3)
}
