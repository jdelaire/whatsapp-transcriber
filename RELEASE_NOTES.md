# Transcriber releases

## 1.2.0, build 3

Published on 2026-11-06. [Release](https://github.com/jdelaire/whatsapp-transcriber/releases/tag/v1.2.0) · [Download Transcriber-1.2.0-arm64.zip](https://github.com/jdelaire/whatsapp-transcriber/releases/download/v1.2.0/Transcriber-1.2.0-arm64.zip).

### Automatic on-device summaries

Transcriber now summarizes new and existing voice-note transcripts automatically, with up to three bullets in the transcript's language. The original transcript appears first and stays unchanged. Use Show original to read it and Copy full original to copy the complete text. Settings lets you pause summaries and retry failed summaries.

Summary generation requires macOS 26 or later with Apple Intelligence ready. Transcription still supports macOS 15 or later on Apple Silicon. Summaries run on your Mac with Apple's Foundation Models, with no cloud service or API key required.

### Validation and distribution

This release is signed with Developer ID, notarized by Apple, and includes a stapled notarization ticket. Release artifact checks cover signatures, the packaged engine, Sparkle's update signature, source materials, licenses, and SHA-256 hashes. See verification.json for the exact checks and limitations.

Before the release rebuild, 82 Python tests and 26 Swift tests passed. Actual on-device English, French, and long-transcript summary probes passed. The final release archive received independent packaging and isolated engine checks. macOS 15 deployment targets were audited, but actual macOS 15 execution remains untested. The installed Sparkle upgrade is a separate test in progress and remains unverified in this report.

Transcriber-SourceMaterials.zip contains matching application source and bundled dependency licenses, including Sparkle's license. SHA256SUMS.txt covers the application ZIP, source ZIP, appcast.xml, and verification.json.

Users of 1.0.0 must install a Sparkle-enabled release manually. Installing 1.2.0 enables future update checks.

See the [release verification report](docs/releases/1.2.0-verification.md) for the published source, hashes, recorded checks and remaining limits.

## 1.0.0

Automatically transcribe incoming WhatsApp voice notes and audio attachments from a native Mac menu bar app. English and French are detected automatically, with local Whisper processing and no paid transcription API.

### Install

Download Transcriber-1.0.0-arm64.zip, extract it, move Transcriber.app to Applications and open it. Click the waveform in the menu bar. First setup downloads a verified 488 MB speech model and links WhatsApp using a phone-number code. Homebrew, Python and Terminal are not required.

Requires Apple Silicon and macOS 15 or later. The app is Developer ID signed, Apple notarized and stapled.

### Included

Recent transcripts with Open and Copy, persistent pause/resume, optional launch at login, private notifications and recovery of an existing command-line installation. Closing the menu keeps capture running. Quitting the app stops capture and its helper processes.

Transcriber-SourceMaterials.zip contains matching application source, pinned upstream helper sources, dependency inventories, build scripts and licenses. SHA256SUMS.txt lists final asset hashes. Verification.json records checks performed on this release.

### Validation and limits

58 Python tests and 7 Swift tests cover application and packaging logic. The release verification file records portable-tool, engine and signature checks. Personal paths are remapped out of binaries and removed from build materials. No linked-device credentials, recordings, chat history, transcripts or private local Git history are included.

Runtime validation was performed on Apple Silicon with macOS 27.2. macOS 15 deployment targets were audited, but execution on macOS 15 remains untested. Automated menu interaction was unavailable during validation. A fresh incoming note after final migration remains a manual check. Speech recognition may mishear names, accents or noise.
