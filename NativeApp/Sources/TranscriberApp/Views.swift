import AppKit
import SwiftUI
import TranscriberCore

struct PanelView: View {
    let model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            switch model.route {
            case .inbox: InboxView(model: model)
            case .setup: SetupView(model: model)
            case .settings: PreferencesView(model: model)
            case .detail(let id): TranscriptView(model: model, id: id)
            }
            if let error = model.error {
                Divider()
                HStack(alignment: .top) {
                    Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
                    Text(error).font(.caption).textSelection(.enabled)
                    Spacer(minLength: 0)
                    Button { model.error = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                }.padding(12)
            }
        }
        .frame(width: 360)
        .background(.background)
        .onAppear { model.panelVisible = true; Task { await model.refresh() } }
        .onDisappear { model.panelVisible = false }
    }
}

struct PanelHeader: View {
    let title: String
    let subtitle: String
    var back: (() -> Void)?
    var settings: (() -> Void)?

    var body: some View {
        HStack(spacing: 11) {
            if let back {
                Button(action: back) { Image(systemName: "chevron.left").frame(width: 24, height: 28) }
                    .buttonStyle(.plain).accessibilityLabel("Back to inbox")
            } else {
                Image(systemName: "waveform").font(.system(size: 19, weight: .medium))
                    .foregroundStyle(.green).frame(width: 34, height: 34)
                    .background(.green.opacity(0.10), in: RoundedRectangle(cornerRadius: 10))
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 15, weight: .medium))
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 4)
            if let settings {
                Button(action: settings) { Image(systemName: "gearshape").frame(width: 28, height: 28) }
                    .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("Settings")
            }
        }.padding(.horizontal, 18).padding(.top, 18).padding(.bottom, 16)
    }
}

struct CaptureStatusView: View {
    let title: String
    let phase: CapturePhase
    let busy: Bool

    var body: some View {
        HStack(spacing: 7) {
            Circle().fill(phase == .connected || phase == .transcribing ? .green : .secondary).frame(width: 7, height: 7)
            Text(title).font(.system(size: 12, weight: .medium))
            Spacer(minLength: 1)
            if busy || phase == .transcribing { ProgressView().controlSize(.mini) }
            else if phase == .connected { Text("Listening").font(.caption2).foregroundStyle(.secondary) }
        }
        .padding(11)
        .background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8))
        .padding(.horizontal, 16).padding(.bottom, 16)
    }
}

struct InboxView: View {
    let model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            PanelHeader(title: "Transcriber", subtitle: "WhatsApp voice notes, in words", settings: { model.route = .settings })
            CaptureStatusView(title: model.statusTitle, phase: model.phase, busy: model.isBusy)
            if model.snapshot.recent.isEmpty {
                EmptyInboxView(needsSetup: !model.canCapture, setup: { model.route = .setup })
            } else {
                HStack {
                    Text("Recent transcripts")
                    Spacer()
                    Text("\(model.snapshot.counts["complete", default: model.snapshot.recent.count]) saved")
                }.font(.caption2).foregroundStyle(.secondary).padding(.horizontal, 18).padding(.bottom, 5)
                ForEach(Array(model.snapshot.recent.prefix(3))) { transcript in
                    TranscriptRow(transcript: transcript, open: { model.openTranscript(id: transcript.id) }, copy: { model.copy(transcript) })
                    if transcript.id != model.snapshot.recent.prefix(3).last?.id { Divider() }
                }
            }
            if let notice = model.notice {
                Text(notice).font(.caption2).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 18).padding(.vertical, 7)
            }
            Divider().padding(.top, 8)
            HStack(spacing: 8) {
                if model.canCapture {
                    Button(model.snapshot.paused ? "Resume transcription" : model.phase == .stopped ? "Resume transcription" : "Pause transcription") {
                        if model.phase == .stopped && !model.snapshot.paused { model.resume() } else { model.togglePause() }
                    }.frame(maxWidth: .infinity)
                } else {
                    Button("Complete setup") { model.route = .setup }.frame(maxWidth: .infinity)
                }
                Button("Open folder") { model.openFolder() }
            }.buttonStyle(.bordered).controlSize(.regular).disabled(model.isBusy).padding(.horizontal, 16).padding(.vertical, 12)
            HStack {
                Text("Processed on this Mac")
                Spacer()
                Button("Quit Transcriber") { NSApplication.shared.terminate(nil) }.buttonStyle(.plain).disabled(model.isBusy)
            }.font(.caption2).foregroundStyle(.secondary).padding(.horizontal, 18).padding(.bottom, 14)
        }
    }
}

struct EmptyInboxView: View {
    let needsSetup: Bool
    let setup: () -> Void

    var body: some View {
        VStack(spacing: 9) {
            Image(systemName: "text.bubble").font(.system(size: 28)).foregroundStyle(.tertiary)
            Text(needsSetup ? "Ready for your first voice note" : "Your next voice note appears here")
                .font(.system(size: 13, weight: .medium))
            Text(needsSetup ? "Download the speech model and link WhatsApp to get started." : "Incoming recordings are transcribed automatically, in their original language.")
                .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
            if needsSetup { Button("Set up Transcriber", action: setup).buttonStyle(.borderedProminent).padding(.top, 5) }
        }.padding(.horizontal, 25).padding(.vertical, 25).frame(maxWidth: .infinity)
    }
}

struct TranscriptRow: View {
    let transcript: Transcript
    let open: () -> Void
    let copy: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(transcript.title).font(.system(size: 13, weight: .medium)).lineLimit(1)
                Spacer()
                Text(transcript.date, style: .relative).font(.caption2).foregroundStyle(.secondary)
            }
            if transcript.summary.state == .ready {
                Text("Summary").font(.caption2).foregroundStyle(.secondary)
            }
            Text(transcript.summary.state == .ready ? transcript.summary.bullets.joined(separator: " · ") : transcript.text)
                .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
            HStack(spacing: 7) {
                Button("Open", action: open)
                Button("Copy", action: copy)
                Spacer()
            }.buttonStyle(.bordered).controlSize(.mini)
        }.padding(.horizontal, 18).padding(.vertical, 12)
    }
}

struct SetupView: View {
    @Bindable var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            PanelHeader(title: "Set up Transcriber", subtitle: "Everything stays on your Mac", back: { model.route = .inbox })
            VStack(alignment: .leading, spacing: 18) {
                ModelSetupView(ready: model.snapshot.modelReady, downloading: model.downloading, progress: model.downloadProgress,
                               download: model.downloadModel, cancel: model.cancelDownload)
                if model.snapshot.authenticated {
                    Label("WhatsApp is linked to this Mac", systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.system(size: 13))
                    if model.snapshot.modelReady {
                        Button("Start transcription") { model.resume(); model.route = .inbox }
                            .buttonStyle(.borderedProminent).disabled(model.isBusy)
                    }
                } else if let code = model.pairingCode {
                    LinkingCodeView(code: code, cancel: model.cancelLink)
                } else {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Link WhatsApp").font(.system(size: 13, weight: .medium))
                        TextField("Phone number with country code", text: $model.phone)
                            .textFieldStyle(.roundedBorder).onSubmit { model.linkPhone() }
                            .disabled(model.isBusy).privacySensitive()
                        Text("Include + and your country code. Your number is used only for linking.")
                            .font(.caption2).foregroundStyle(.secondary)
                        HStack {
                            Button(model.isBusy ? "Requesting code…" : "Get linking code") { model.linkPhone() }
                                .buttonStyle(.borderedProminent).disabled(model.isBusy || model.phone.trimmingCharacters(in: .whitespaces).isEmpty)
                            if model.isBusy && !model.downloading { Button("Cancel") { model.cancelLink() } }
                        }.padding(.top, 4)
                    }
                }
            }.padding(.horizontal, 18).padding(.bottom, 20)
        }
    }
}

struct ModelSetupView: View {
    let ready: Bool
    let downloading: Bool
    let progress: Double
    let download: () -> Void
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Speech model").fontWeight(.medium)
                Spacer()
                Text(ready ? "Ready" : "488 MB").foregroundStyle(.secondary)
            }.font(.system(size: 12))
            Text("English and French detected automatically. Download once, transcribe locally.")
                .font(.caption2).foregroundStyle(.secondary)
            if downloading {
                ProgressView(value: progress)
                HStack {
                    Text(progress >= 1 ? "Verifying download…" : progress.formatted(.percent.precision(.fractionLength(0))))
                        .font(.caption2).foregroundStyle(.secondary)
                    Spacer()
                    Button("Cancel", action: cancel).controlSize(.mini)
                }
            } else if !ready { Button("Download model", action: download).controlSize(.small) }
        }.padding(13).background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 9))
    }
}

struct LinkingCodeView: View {
    let code: String
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Enter this code on your phone").font(.system(size: 13, weight: .medium))
            Text(code).font(.system(size: 25, weight: .medium, design: .monospaced)).tracking(2)
                .textSelection(.enabled).frame(maxWidth: .infinity).padding(.vertical, 12)
                .background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8)).privacySensitive()
            Text("WhatsApp Settings › Linked devices › Link a device › Link with phone number")
                .font(.caption).foregroundStyle(.secondary)
            HStack { ProgressView().controlSize(.small); Text("Waiting for your phone…").font(.caption); Spacer(); Button("Cancel", action: cancel).controlSize(.small) }
        }
    }
}

struct PreferencesView: View {
    let model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            PanelHeader(title: "Settings", subtitle: "Transcriber", back: { model.route = .inbox })
            VStack(spacing: 14) {
                VStack(spacing: 0) {
                    Toggle(isOn: Binding(get: { model.launchAtLogin }, set: model.setLogin)) {
                        SettingsLabel(title: "Launch at login", subtitle: "Ready when you start your Mac")
                    }.toggleStyle(.switch).controlSize(.small).padding(12)
                    Divider().padding(.horizontal, 12)
                    Toggle(isOn: Binding(get: { model.notifications }, set: model.setNotifications)) {
                        SettingsLabel(title: "Notify when ready", subtitle: "Keep message contents private")
                    }.toggleStyle(.switch).controlSize(.small).padding(12)
                }.background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8)).disabled(model.isBusy)
                VStack(spacing: 0) {
                    SettingsValue(title: "Language", value: "Detect automatically")
                    Divider().padding(.horizontal, 12)
                    SettingsValue(title: "Speech model", value: model.snapshot.modelReady ? "Downloaded · 488 MB" : "Not downloaded")
                    Divider().padding(.horizontal, 12)
                    HStack { Text("Transcripts folder"); Spacer(); Button("Open") { model.openFolder() }.controlSize(.mini) }.font(.system(size: 12)).padding(12)
                }.background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8)).disabled(model.isBusy)
                HStack {
                    SettingsLabel(title: "WhatsApp", subtitle: model.snapshot.authenticated ? "Linked to this Mac" : "Link required")
                    Spacer()
                    Button(model.snapshot.authenticated ? "Connection" : "Link") { model.route = .setup }.controlSize(.small)
                }.padding(12).background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8)).disabled(model.isBusy)
                UpdateSettingsView(updates: model.updates)
                    .disabled(model.isBusy)
                SummarySettingsView(model: model)
                Text("Closing this panel keeps transcription running. Quitting the app stops capture.")
                    .font(.caption).foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
            }.padding(.horizontal, 18).padding(.bottom, 20)
        }
    }
}

struct UpdateSettingsView: View {
    let updates: UpdateController

    var body: some View {
        VStack(spacing: 0) {
            Toggle(isOn: Binding(get: { updates.automaticChecks }, set: updates.setAutomaticChecks)) {
                SettingsLabel(title: "Check for updates automatically", subtitle: "Ask before installing")
            }.toggleStyle(.switch).controlSize(.small).padding(12)
            Divider().padding(.horizontal, 12)
            HStack {
                Text("Version \(Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Development")")
                    .foregroundStyle(.secondary)
                Spacer()
                Button(updates.updateAvailable ? "Update available…" : "Check for Updates…", action: updates.checkForUpdates)
                    .controlSize(.small).disabled(!updates.canCheckForUpdates)
            }.font(.system(size: 12)).padding(12)
        }.background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct SettingsLabel: View {
    let title: String
    let subtitle: String
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.system(size: 12))
            Text(subtitle).font(.caption2).foregroundStyle(.secondary)
        }
    }
}

struct SettingsValue: View {
    let title: String
    let value: String
    var body: some View {
        HStack { Text(title); Spacer(); Text(value).foregroundStyle(.secondary) }.font(.system(size: 12)).padding(12)
    }
}

struct TranscriptView: View {
    let model: AppModel
    let id: String
    var body: some View {
        VStack(spacing: 0) {
            PanelHeader(title: model.transcriptVisit.detail?.title ?? "Transcript", subtitle: model.transcriptVisit.detail?.date.formatted(date: .abbreviated, time: .shortened) ?? "", back: { model.route = .inbox })
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if model.transcriptVisit.loading { ProgressView().controlSize(.small) }
                    if let detail = model.transcriptVisit.detail {
                        if detail.summary.state == .ready {
                            HStack {
                                Text(model.transcriptVisit.showingSummary ? "Summary" : "Original transcript").font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                Button(model.transcriptVisit.showingSummary ? "Show original" : "Show summary") {
                                    model.transcriptVisit.select(model.transcriptVisit.showingSummary ? .original : .summary)
                                }.controlSize(.small)
                            }
                        } else if let status = detail.summary.statusMessage(enabled: model.snapshot.summarySettings.enabled, availability: model.summaryAvailability) {
                            Text(status).font(.caption).foregroundStyle(.secondary)
                            if detail.summary.state == .failed {
                                Button("Retry summary") { model.retrySummary(id: id) }.controlSize(.small)
                                    .disabled(!model.snapshot.summarySettings.enabled || model.summaryAvailability != .available)
                            }
                        }
                        if let text = model.transcriptVisit.displayedText {
                            Text(text).font(.system(size: 13)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    if let error = model.transcriptVisit.error {
                        Text(error).font(.caption).foregroundStyle(.secondary)
                        Button("Retry") { model.openTranscript(id: id) }.controlSize(.small)
                    }
                }.padding(.horizontal, 18).padding(.bottom, 16)
            }.frame(minHeight: 180, maxHeight: 380)
            Divider()
            HStack {
                Button("Copy transcript") { model.copyTranscript(id: id) }.frame(maxWidth: .infinity)
                    .disabled(model.transcriptVisit.detail?.text == nil)
                Button("Done") { model.route = .inbox }
            }.buttonStyle(.bordered).padding(16)
        }
    }
}

struct SummarySettingsView: View {
    let model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Toggle(isOn: Binding(get: { model.snapshot.summarySettings.enabled }, set: model.setAutomaticSummaries)) {
                SettingsLabel(title: "Automatic summaries", subtitle: "Includes saved voice notes")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }.toggleStyle(.switch).controlSize(.small).disabled(model.summaryPreferenceSaving)
            Text("Apple Intelligence summarizes on this Mac. Original transcripts stay intact.")
                .font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let error = model.summarySettingsError {
                Text(error).font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } else if !model.snapshot.summarySettings.enabled {
                Text("Summaries paused. Saved summaries remain available.").font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } else if case .unavailable(let reason) = model.summaryAvailability {
                Text(reason.explanation).font(.caption2).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            } else {
                Text("\(model.snapshot.summaryCounts["ready", default: 0]) of \(model.snapshot.summaryCounts["total", default: 0]) summarized")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
            .background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8))
    }
}
