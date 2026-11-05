# Transcriber 1.0.0

Automatically transcribe incoming WhatsApp voice notes and audio attachments from a native Mac menu bar app. English and French are detected automatically, with local Whisper processing and no paid transcription API.

## Install

Download Transcriber-1.0.0-arm64.zip, extract it, move Transcriber.app to Applications and open it. Click the waveform in the menu bar. First setup downloads a verified 488 MB speech model and links WhatsApp using a phone-number code. Homebrew, Python and Terminal are not required.

Requires Apple Silicon and macOS 15 or later. The app is Developer ID signed, Apple notarized and stapled.

## Included

Recent transcripts with Open and Copy, persistent pause/resume, optional launch at login, private notifications and recovery of an existing command-line installation. Closing the menu keeps capture running. Quitting the app stops capture and its helper processes.

Transcriber-SourceMaterials.zip contains matching application source, pinned upstream helper sources, dependency inventories, build scripts and licenses. SHA256SUMS.txt lists final asset hashes. Verification.json records checks performed on this release.

## Validation and limits

58 Python tests and 7 Swift tests cover application and packaging logic. The release verification file records portable-tool, engine and signature checks. Personal paths are remapped out of binaries and removed from build materials. No linked-device credentials, recordings, chat history, transcripts or private local Git history are included.

Runtime validation was performed on Apple Silicon with macOS 27.2. macOS 15 deployment targets were audited, but execution on macOS 15 remains untested. Automated menu interaction was unavailable during validation. A fresh incoming note after final migration remains a manual check. Speech recognition may mishear names, accents or noise.
