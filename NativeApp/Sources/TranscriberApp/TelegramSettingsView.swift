import SwiftUI

struct TelegramSettingsRow: View {
    let chatID: String?
    let open: () -> Void

    var body: some View {
        HStack {
            SettingsLabel(title: "Telegram", subtitle: chatID.map { "Sending to chat \($0)" } ?? "Off")
            Spacer()
            Button(chatID == nil ? "Set up" : "Manage", action: open).controlSize(.small)
        }.padding(12).background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct TelegramView: View {
    let model: AppModel
    @State private var changing = false

    var body: some View {
        VStack(spacing: 0) {
            PanelHeader(title: "Telegram", subtitle: "Optional delivery", back: { model.route = .settings }, backLabel: "Back to settings")
            VStack(alignment: .leading, spacing: 14) {
                Text("Send new WhatsApp transcripts to this Telegram chat. The full text arrives first, followed by the on-device summary when it’s ready.")
                    .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if let chatID = model.telegramChatID, !changing {
                    TelegramConnectedView(chatID: chatID, saving: model.telegramSaving,
                                          change: { changing = true }, disconnect: model.disconnectTelegram)
                } else {
                    TelegramCredentialsForm(model: model, initialChatID: model.telegramChatID ?? "",
                                            cancel: model.telegramChatID == nil ? nil : { changing = false },
                                            saved: { changing = false })
                }
                if let status = model.telegramDeliveryStatus {
                    Label(status, systemImage: "exclamationmark.circle")
                        .font(.caption2).foregroundStyle(.secondary).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }.padding(.horizontal, 18).padding(.bottom, 20)
        }
    }
}

struct TelegramConnectedView: View {
    let chatID: String
    let saving: Bool
    let change: () -> Void
    let disconnect: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Connected", systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.system(size: 13))
            HStack { Text("Chat ID"); Spacer(); Text(chatID).foregroundStyle(.secondary).textSelection(.enabled).privacySensitive() }.font(.system(size: 12))
            HStack {
                Button("Change bot or chat", action: change)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("Disconnect", role: .destructive, action: disconnect)
            }.controlSize(.small).disabled(saving).padding(.top, 2)
        }.padding(13).background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 9))
    }
}

struct TelegramCredentialsForm: View {
    let model: AppModel
    let cancel: (() -> Void)?
    let saved: () -> Void
    @State private var token = ""
    @State private var chatID: String
    @State private var submittedChatID: String?

    init(model: AppModel, initialChatID: String, cancel: (() -> Void)?, saved: @escaping () -> Void) {
        self.model = model
        self.cancel = cancel
        self.saved = saved
        _chatID = State(initialValue: initialChatID)
    }

    private var trimmedToken: String { token.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var trimmedChatID: String { chatID.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var chatIDValid: Bool {
        let digits = trimmedChatID.hasPrefix("-") ? trimmedChatID.dropFirst() : Substring(trimmedChatID)
        return (1...16).contains(digits.count) && digits.first != "0" && digits.allSatisfy { $0.isASCII && $0.isNumber }
    }
    private var canSave: Bool { !model.telegramSaving && !trimmedToken.isEmpty && chatIDValid }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Bot token").font(.system(size: 12, weight: .medium))
            SecureField("Paste the token from BotFather", text: $token)
                .textFieldStyle(.roundedBorder).privacySensitive().onSubmit(save)
            Text(cancel == nil ? "Saved tokens are never shown again." : "Enter the token again to save changes.")
                .font(.caption2).foregroundStyle(.secondary)
            Text("Chat ID").font(.system(size: 12, weight: .medium)).padding(.top, 4)
            TextField("123456789 or -100123456789", text: $chatID)
                .textFieldStyle(.roundedBorder).privacySensitive().onSubmit(save)
            Text(!trimmedChatID.isEmpty && !chatIDValid ? "Use digits only. Group IDs start with a minus sign." : "For a private chat, open your bot in Telegram and press Start first.")
                .font(.caption2).foregroundStyle(!trimmedChatID.isEmpty && !chatIDValid ? AnyShapeStyle(.orange) : AnyShapeStyle(.secondary))
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Button(model.telegramSaving ? "Saving…" : "Save", action: save).buttonStyle(.borderedProminent).disabled(!canSave)
                if let cancel { Button("Cancel", action: cancel).disabled(model.telegramSaving) }
                if model.telegramSaving { ProgressView().controlSize(.small) }
            }.padding(.top, 4)
        }
        .disabled(model.isBusy)
        .onChange(of: model.telegramSaving) { finishIfSaved() }
        .onChange(of: model.telegramChatID) { finishIfSaved() }
        .onDisappear { token = "" }
    }

    private func save() {
        guard canSave else { return }
        let submitted = trimmedChatID
        submittedChatID = submitted
        model.configureTelegram(token: trimmedToken, chatID: submitted)
        token = ""
        finishIfSaved()
    }

    private func finishIfSaved() {
        guard let submittedChatID, !model.telegramSaving, model.telegramSettingsError == nil,
              model.telegramChatID == submittedChatID else { return }
        self.submittedChatID = nil
        saved()
    }
}
