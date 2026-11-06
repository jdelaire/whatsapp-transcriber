import Foundation
import Testing
@testable import TranscriberCore

@Suite struct SummaryPresentation {
    private func detail(_ id: String = "a", state: SummaryState = .ready, fingerprint: String = "one") -> TranscriptDetail {
        .init(id: id, title: "Example", timestamp: "2026-01-01T00:00:00Z", path: "", text: String(repeating: "Full original\n", count: 100000), sourceHash: "hash", sourceFingerprint: fingerprint, summary: .init(state: state, bullets: ["Short summary"], sourceHash: "hash", sourceFingerprint: fingerprint))
    }

    @Test func originalSelectionSurvivesRefreshAndResetsOnNextVisit() {
        var visit = TranscriptVisit()
        let token = visit.open(id: "a")
        let mutation1 = visit.receive(detail(), requestedID: "a", token: token)
        #expect(mutation1)
        #expect(visit.showingSummary)
        visit.select(.original)
        let refreshed = detail()
        let mutation2 = !visit.update(refreshed.summary, requestedID: "a", token: token)
        #expect(mutation2)
        #expect(!visit.showingSummary)
        #expect(visit.displayedText == refreshed.text)
        let next = visit.open(id: "a")
        let mutation3 = visit.receive(refreshed, requestedID: "a", token: next)
        #expect(mutation3)
        #expect(visit.showingSummary)
    }

    @Test func staleResponsesCannotOverwriteAnotherVisitOrReopenedNote() {
        var visit = TranscriptVisit()
        let a = visit.open(id: "a")
        let b = visit.open(id: "b")
        let mutation4 = !visit.receive(detail(), requestedID: "a", token: a)
        #expect(mutation4)
        let mutation5 = visit.receive(detail("b"), requestedID: "b", token: b)
        #expect(mutation5)
        let reopened = visit.open(id: "a")
        let mutation6 = !visit.receive(detail(), requestedID: "a", token: a)
        #expect(mutation6)
        let mutation7 = visit.receive(detail(), requestedID: "a", token: reopened)
        #expect(mutation7)
        visit.select(.original)
        let mutation8 = !visit.update(.init(), requestedID: "a", token: a)
        #expect(mutation8)
        #expect(visit.selection == .original)
    }

    @Test func unchangedStatusDoesNotRequestBodyReadsButChangedSourceDoes() {
        var visit = TranscriptVisit()
        let token = visit.open(id: "a")
        _ = visit.receive(detail(state: .pending), requestedID: "a", token: token)
        let mutation9 = !visit.update(detail(state: .pending).summary, requestedID: "a", token: token)
        #expect(mutation9)
        let mutation10 = visit.update(detail(state: .pending, fingerprint: "two").summary, requestedID: "a", token: token)
        #expect(mutation10)
        _ = visit.receive(detail(state: .pending, fingerprint: "two"), requestedID: "a", token: token)
        let mutation11 = !visit.update(detail(state: .pending, fingerprint: "two").summary, requestedID: "a", token: token)
        #expect(mutation11)
        _ = visit.update(.init(state: .failed, errorCode: "original_missing"), requestedID: "a", token: token)
        #expect(visit.detail?.text == nil)
    }

    @Test func readyCacheStaysVisibleWhenDisabledOrUnavailable() {
        let ready = SummaryRecord(state: .ready, bullets: ["Saved"])
        #expect(ready.statusMessage(enabled: false, availability: .unavailable(.modelNotReady)) == nil)
        #expect(SummaryRecord().statusMessage(enabled: false, availability: .available) != nil)
        #expect(SummaryRecord().statusMessage(enabled: true, availability: .unavailable(.requiresMacOS26)) != nil)
    }
}
