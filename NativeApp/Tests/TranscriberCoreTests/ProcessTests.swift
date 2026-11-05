import Darwin
import Foundation
import Testing
@testable import TranscriberCore

@Test func invalidEngineEventStopsItsProcess() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let executable = root.appendingPathComponent("Contents/Helpers/Engine.app/Contents/MacOS/WAEngine")
    let marker = root.appendingPathComponent("pid")
    try FileManager.default.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
    let script = "#!/bin/sh\necho $$ > '\(marker.path)'\nprintf 'invalid-json\\n'\nexec /bin/sleep 60\n"
    try script.write(to: executable, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
    let process = EngineProcess(location: EngineLocation(bundle: root), command: "status")
    defer { process.stop() }
    let began = Date.now
    #expect(throws: (any Error).self) { try process.stream { _ in } }
    #expect(Date.now.timeIntervalSince(began) < 3)
    let pid = try #require(Int32(String(contentsOf: marker, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)))
    #expect(kill(pid, 0) == -1)
}

@Test func exitedEngineCannotLeaveDetachedHelpersRunning() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let executable = root.appendingPathComponent("Contents/Helpers/Engine.app/Contents/MacOS/WAEngine")
    let marker = root.appendingPathComponent("heartbeat")
    let pidFile = root.appendingPathComponent("child")
    try FileManager.default.createDirectory(at: executable.deletingLastPathComponent(), withIntermediateDirectories: true)
    let script = """
    #!/usr/bin/python3
    import json,os,signal,time
    child=os.fork()
    if not child:
        os.setsid()
        signal.signal(signal.SIGTERM,signal.SIG_IGN)
        os.close(1)
        os.close(2)
        open('\(pidFile.path)','w').write(str(os.getpid()))
        while True:
            open('\(marker.path)','w').write(str(time.time_ns()))
            time.sleep(0.02)
    while not os.path.exists('\(pidFile.path)'):
        time.sleep(0.01)
    print(json.dumps({'event':'child_started','group':child}),file=__import__('sys').stderr,flush=True)
    print(json.dumps({'event':'ready'}),flush=True)
    """
    try script.write(to: executable, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
    defer {
        if let value = try? String(contentsOf: pidFile, encoding: .utf8), let pid = Int32(value) { kill(-pid, SIGKILL) }
    }
    let process = EngineProcess(location: EngineLocation(bundle: root), command: "run")
    #expect(try process.stream { _ in } == 0)
    Thread.sleep(forTimeInterval: 0.1)
    let heartbeat = try Data(contentsOf: marker)
    Thread.sleep(forTimeInterval: 0.2)
    #expect(try Data(contentsOf: marker) == heartbeat)
}
