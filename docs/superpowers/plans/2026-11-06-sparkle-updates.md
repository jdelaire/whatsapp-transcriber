# Sparkle updates implementation plan

Goal: add native, signed macOS app updates without changing the local transcription engine.

Architecture: retain one Sparkle 2.10.1 controller in the app model. Use Sparkle's scheduler, preferences and standard update UI. Embed its framework in the custom bundle and prepare signed GitHub release artifacts locally.

Constraints: macOS 15+, Apple Silicon, no automatic installation, no code comments, no remote publishing. Keep credentials, model, queue and transcripts outside the app. Preview and isolated runs never start the updater or change update preferences.

Sol 6.1 high reviewed this plan before implementation. The review required explicit nested framework signing, immutable enclosure URLs, final-archive signing after stapling and a visible reminder for this background app.

## Tasks

- [x] Add exact SPM dependency and lockfile. Retain one controller. Expose Settings check button, automatic-check toggle and menu-bar update indicator. Preserve existing asynchronous engine shutdown.
- [x] Embed the resolved framework, set runtime lookup path, remove build paths and sign XPC services, Autoupdate, Updater.app, framework and outer app in order. Preserve Downloader entitlements. Include license.
- [x] Pin public key and feed. Keep private key in Keychain. Parameterize version/build/output. Prepare notarized/stapled final ZIP, official generated appcast, signature verification and hashes without publishing.
- [x] Verify release validation rejects mismatched metadata/signatures and invalid version paths. Verify universal binary dependency parsing. Run Python/Swift suites and inspect signed extracted archive.
- [x] Capture Settings before/after and exercise updater UI. Obtain independent Sol 6.1 high bug/performance review. Fix findings. Merge to main, delete task branch, commit single change.

## Review focus

Background update reminders must remain visible. Preview runs must avoid network and preference writes. Existing quit handling must stop owned engine processes during update installation. Release preparation must fail without the pinned signing key or Developer ID. Feed metadata must describe the exact final archive.

## Verification limits

The live GitHub feed requires publication of the first Sparkle-enabled release. An actual installed-version upgrade remains a release check. Existing 1.0.0 installations require a manual first upgrade.

Verification: 62 Python and 7 Swift tests passed; Developer ID bundle and extracted ZIP passed strict signing checks; official appcast generator and verifier accepted final QA archive. Native Settings before/after previews showed no clipping and both preview processes exited. Sol review found only missing license in source staging, corrected before packaging verification.
