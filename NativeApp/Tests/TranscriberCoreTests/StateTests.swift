import Foundation
import Testing
@testable import TranscriberCore

@Test func staleWorkerIsNeverShownAsListening() throws {
    var state = EngineSnapshot.empty
    state.configured = true
    state.modelReady = true
    state.authenticated = true
    state.workerAlive = false
    state.connection = "connected"
    #expect(state.phase == .stopped)
}

@Test func temporaryAuthenticationFailureDoesNotPromptRelinking() {
    var state = EngineSnapshot.empty
    state.configured = true
    state.modelReady = true
    state.authenticationError = "Temporarily unavailable"
    #expect(state.phase == .checking)
}

@Test func pauseTakesPriorityOverConnection() {
    var state = EngineSnapshot.empty
    state.configured = true
    state.modelReady = true
    state.authenticated = true
    state.workerAlive = true
    state.connection = "connected"
    state.paused = true
    #expect(state.phase == .paused)
}

@Test func fragmentedPairingEventsDecodeWithoutPhoneField() throws {
    var decoder = EventDecoder()
    #expect(try decoder.append(Data("{\"schema_version\":1,\"event\":\"pair_".utf8)).isEmpty)
    let events = try decoder.append(Data("code\",\"code\":\"DEMO-CODE\",\"phone\":\"private\"}\n{\"event\":\"paired\"}\n".utf8))
    #expect(events.count == 2)
    #expect(events[0].code == "DEMO-CODE")
    #expect(events[1].event == "paired")
}

@Test func modelVerificationRejectsWrongBytesAndDigest() {
    let data = Data("abc".utf8)
    let digest = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    #expect(ModelVerification.matches(data, bytes: 3, sha256: digest))
    #expect(!ModelVerification.matches(data, bytes: 4, sha256: digest))
    #expect(!ModelVerification.matches(data, bytes: 3, sha256: "bad"))
}
