# On-device voice-note summaries

Draft for design approval, November 6, 2026. No implementation plan or product changes yet. The user chose automatic summaries for every new and existing transcript, up to three bullets, one for short notes, and summary-first display with Show original. All other defaults below are builder proposals.

## Product behavior

Every completed voice note becomes eligible, including the entire existing completed-transcript library. Transcription publishes its original `.txt` first. Capture, transcript notifications, copying, and navigation never await generation.

Inbox and detail show the saved summary by default, labeled Summary. Detail offers Show original and Show summary. Without a valid summary, display the original with pending, unavailable, disabled, or failed status. Resolve detail by transcript ID so completed summaries appear without reopening. Preserve an explicit Show original selection during that visit.

Generate one concise bullet for bodies of at most 280 Unicode characters; otherwise one to three. Use the transcript's dominant language, preserving names, dates, amounts, requests, and uncertainty. Mixed-language notes may retain original terms. Never invent facts or translate by default. Label generated content; do not promise factual accuracy. Copy transcript always copies the full original. Exclude summary export, chat replies, model selection, and length controls.

Settings adds **Automatic summaries**, initially On for existing and new installations, with text explaining that saved notes are included and processing uses Apple Intelligence on this Mac.

- On starts or resumes pending work and catches up all readable completed transcripts, including notes completed while Off. Unchanged failed attempts remain failed until Retry; source or prompt changes create new eligible work.
- Off persists immediately, atomically invalidates durable attempts and increments a settings revision, requests cancellation of the active attempt, and prevents new claims. Notify the owning worker across app instances. Recheck enabled state, revision, and attempt token before each chunk/reduction request and completion. Do not start another native request after Off/On until the previous request actually settles. Queued work remains; saved summaries still display. Retry requires On and an available model.
- An unavailable model preserves the switch's intention and pending work. Explain the reason in Settings and resume automatically when available. No separate backfill confirmation or retry-all control.

Keep the macOS 15 baseline. Generation requires macOS 26+ and available `SystemLanguageModel`; persisted summaries remain readable on older systems. Handle `deviceNotEligible`, `appleIntelligenceNotEnabled`, `modelNotReady`, and unknown reasons. Recheck at launch, wake/app activation, and the existing low-frequency refresh, even with the panel closed. Closing the panel continues work; quitting stops it. Pausing transcription does not pause summaries of already completed notes.

## Approach and ownership

Automatic persisted summaries cover notes nobody opens; on-demand generation would not. Bullets are user-selected over paragraphs.

Recommend a native Swift worker with persistence through the existing Python engine. Swift owns app lifetime and calls FoundationModels directly. A Python-launched Swift helper adds packaging and process/cancellation ownership. Python retains transcript access and SQLite writes. Swift must not write originals or create another database.

`app_state.snapshot()` selects ten completed jobs, reads only 65,536 characters; `InboxView` displays three. `Transcript.text` cannot supply generation or full originals. Active-receiver events cannot establish complete history.

Extend the engine's JSON command protocol with these small contracts, names provisional:

- `transcript(id)` returns the complete body and source hash from a completed job's validated `.txt` path, for detail and copying. Strip only the known metadata header for display/model input. Never cap or rewrite originals.
- Summary operations reconcile completed jobs in resumable pages, claim one body, finish an attempt, retry one failure, and read/write the setting. Snapshot includes compact summary state; detail fetches current state by ID. Extend `EngineClient`/`EngineProcess.collect` for JSON stdin payloads, never transcript arguments. Whole bodies use request collection, bypassing `EventDecoder`'s 1 MiB event limit.

Add `summaries` to existing `jobs.sqlite`, keyed by canonical `jobs.key`, with source hash, last-validated file fingerprint, prompt version, pending/running/ready/failed state, bullets, safe error code, attempt token, and update time. Store setting and scan cursor in existing `metadata`. Separate schema and prompt versions. Preserve existing job/alias/capture semantics. Remove orphan summaries after identity merges; never serve missing/non-complete jobs.

## Catch-up, freshness, and lifetime

Reconcile at startup, enabling, and prompt changes; events accelerate discovery. Periodic scans catch missed completions and file edits, in proposed batches of 25. Use keyset pages over a finite range, persist progress, then wrap to catch jobs completing behind the cursor. Do not restrict history to snapshots, load every body into Swift, or copy/hash the whole database per summary. Index pending selection; hash files outside write transactions.

Python hashes the complete body's UTF-8 bytes with SHA-256, without whitespace normalization. Validate content hash and prompt version on discovery, claim, and finish. Persist the file identity/change fingerprint with the validated hash. Cached snapshot/detail reads compare the current file fingerprint and prompt version without rereading entire bodies. Changed or uncertain files hide cached summaries until background hashing completes; reconciliation persists replacements, including failures. Reads remain side-effect-free. Python commands are short-lived, so caching must survive those process boundaries rather than relying on a per-command Python dictionary. OS/app updates alone preserve valid summaries; bump prompt version when instructions or generation policy require regeneration.

One utility-priority Swift actor, outside `MainActor` and `AppModel.isBusy`, processes durable work, prioritizing new completions over historical backfill, with FIFO order within each class. Persist the feature first-enabled time to distinguish historical jobs from subsequent completions, including those received while summaries were Off. Allow one model request at a time, including chunks/reductions, with an advisory worker lock per data root preventing duplicate app workers. Persist claims before inference. Accept completion only for current token, hash, prompt version, and enabled setting. Keep reads and inference outside database transactions.

After acquiring the lock on launch, return abandoned running work to pending. Cancellation, unavailability, and quit are interruptions, not failures. In `AppModel.shutdown()`, invalidate the local completion epoch immediately, request summary cancellation, and stop capture concurrently. Allow summary persistence/cleanup at most two seconds; never await native generation indefinitely. Capture cleanup must finish before `client.cancelAll()` or process exit. Reject late callbacks through both local shutdown epoch and durable attempt token. Keep the worker lock until native work settles or the process exits. If persistence times out, next launch recovers the interrupted claim. Restart interrupted notes from full text without persisted intermediate sessions. Crashes may repeat computation but cannot publish stale results.

## Generation and failures

Use only `SystemLanguageModel.default`, no cloud fallback or tools. Summary text stays local; never include transcript content in logs, telemetry, update requests, or model-feedback uploads. Model download may require network; generation works offline once available. [Apple's FoundationModels overview](https://developer.apple.com/documentation/foundationmodels) describes the on-device framework.

Use fresh `LanguageModelSession` instances for each independent note, chunk, and reduction. Instructions treat transcript text as quoted data, never executable instructions. Guided generation returns a bullet array; validate count and nonempty strings before persistence. Use default guardrails. Unsupported-language errors, refusals, missing/unreadable originals, and invalid/empty generation produce a note-level explanation with Retry. Preserve the original and continue other notes. Do not silently translate, relax guardrails, or automatically loop failed generations. Check transcript-language support rather than assuming the Mac's locale describes every note. [Apple's language guidance](https://developer.apple.com/documentation/foundationmodels/supporting-languages-and-locales-with-foundation-models) covers support checks and generation errors.

Long inputs require full-text chunk-and-combine. Count instructions, schema, input, and reserved output within `contextSize`, with 512 output tokens and a safety margin as starting defaults. Installed SDK declares `tokenCount(for:)` only on macOS 26.4+ and back-deploys `contextSize` to 26.0. On 26.0–26.3 use small UTF-8 chunks, initially at most 2 KiB, as a heuristic, not a token guarantee. On context overflow split smaller and retry within the same attempt. Prefer sentence boundaries, splitting oversized sentences without dropping text. Extract bounded facts from every chunk, then combine ordered groups recursively until one final summary fits. Every stage reserves its output budget. Bound splitting and require reduction progress; if any stage fails, save no partial summary and offer Retry. No silent truncation. Hierarchical compression can lose detail, so the original remains essential. [Apple's context guidance](https://developer.apple.com/documentation/foundationmodels/generating-content-and-performing-tasks-with-foundation-models) documents the shared 4,096-token budget and chunking approach.

## Builder check

An independent Sol 6.1 high review found three gaps: unbounded shutdown, Off propagation between app instances, and repeated hashing in snapshot reads. The design now bounds summary cleanup to two seconds, invalidates attempts through a shared settings revision, and persists validated file fingerprints. No blocking user-owned question remains. Addressed objections: snapshot backfill misses history, preview input loses text, late results defeat Off, running state can strand work, reducers can loop. Tradeoffs remain slower backfill, extra long-note inference, and restarting interrupted notes.

Meaningful verification should cover more than ten completed notes, completion behind the scan cursor, full bodies beyond both existing caps, unchanged originals and read-only snapshots, source/prompt invalidation, alias merges, FIFO/single-request behavior, Off/On races, cancellation/restart, unavailable/resume, manual retry, and complete multilingual chunk coverage with reducer progress. Measure existing snapshot database-copy cost at realistic library size. Use fake model responses for queue logic, then device-check English/French, short/long notes, unsupported input, macOS 15 fallback, and unavailable states. Do not test exact generated wording, add implementation-mirroring tests, or write code comments.

Next step is user review of this design and its builder defaults. Coding and an implementation plan follow only after design approval.
