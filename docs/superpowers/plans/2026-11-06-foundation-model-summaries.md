# Foundation Models summaries implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. The parent implements all product code. Delegate nonimplementation checks and independent reviews only. Steps use checkbox syntax for tracking.

**Goal:** Automatically save local, same-language summaries for every new and existing completed voice-note transcript, with summary-first display and intact full originals.

**Architecture:** One utility-priority Swift actor owns inference and an advisory lock per data root. The existing Python CLI owns transcript access, durable claims, settings, and summaries in `jobs.sqlite`. FoundationModels stays behind an available-on-macOS-26 adapter; macOS 15 can still display saved summaries.

**Tech stack:** Swift 6, SwiftUI/AppKit, FoundationModels, Python standard library, SQLite, existing JSON process transport and Swift Testing/unittest.

**Spec:** `docs/superpowers/specs/2026-11-06-foundation-model-summaries-design.md`, approved November 6, 2026. Read both documents before implementation.

## Global constraints

- Every completed voice note is eligible, including the entire existing library and completions while Off. Do not use the ten-item snapshot or three-item inbox as the work queue.
- One bullet for bodies of at most 280 Unicode scalar values, otherwise one to three. Preserve dominant language, names, dates, amounts, requests, and uncertainty. Never translate by default.
- Label generated content `Summary`; default to it when valid. Preserve `Show original` during the current detail visit. `Copy transcript` always copies the complete original body.
- `Automatic summaries` defaults On. Saved summaries remain visible when Off or unavailable. No model, length, export, reply, retry-all, or backfill-confirmation controls.
- Generation requires macOS 26+ and available `SystemLanguageModel.default`. Keep deployment target macOS 15.0. No cloud API, tools, relaxed guardrails, model feedback, or transcript logging.
- Capture, original `.txt` publication, notifications, navigation, and copying never await inference. Panel closure and transcription pause do not stop summary work.
- Summary cleanup gets at most two seconds during quit. Never await native generation indefinitely. Capture cleanup must finish before `client.cancelAll()` or process exit.
- No code comments. Test meaningful application logic, not forwarding calls, source text, or exact model wording. No new dependency or generic configuration framework.
- Target local QA at version `1.2.0`, build `3`. Keep released `v1.1.0`, build `2`, its assets, and Sparkle feed immutable. Do not publish, upload, install an update, or run release preparation.
- Parent owns implementation. Fresh Sol 6.1 high reviews this plan before code and bugs/performance after code. Delegate native GUI work to Terra 6.1 xhigh with full access. No simulator work.

## Review focus

1. Replaced files with unchanged sizes or timestamps, symlink escapes, and handwritten text with blank lines must never expose a stale summary or lose original text. Task 1 tests fingerprint changes, validated paths, and exact body extraction.
2. A job completing behind the persisted scan cursor, or while summaries are Off, must eventually run and outrank historical work. Task 2 tests wrapping and durable priority/FIFO selection.
3. A second app toggling Off then On while native cancellation is ignored must invalidate the result and must not overlap the next request. Tasks 2 and 4 test revision/token rejection and an uncooperative model.
4. Emoji, combining marks, long sentences, and a reducer that expands its input must preserve full input coverage or fail explicitly. Task 3 tests scalar thresholds, UTF-8 boundaries, and bounded progress.
5. A detail view already showing an original, a deleted source, or an unavailable model must retain predictable display/copy behavior. Tasks 1 and 5 test missing originals, live detail refresh, and visit-scoped selection.

## Files and contracts

Create `app_summaries.py` for migration, validated full-body access, cache projection, and durable summary operations. It must not import `app_state` or `transcriber`; accept paths/connections. Modify `transcriber.py` only for migration invocation, completion timestamps, and alias-merge cleanup. Modify `app_engine.py` for JSON commands and `app_state.py` for read-only compact summary projection.

Create these focused core files under `NativeApp/Sources/TranscriberCore/`:

- `SummaryTypes.swift`: wire values, safe errors, availability, model/store protocols, and visit-scoped transcript presentation state.
- `SummaryGenerator.swift`: serial full-text chunk/extract/reduce policy, independent of FoundationModels.
- `FoundationSummaryModel.swift`: all FoundationModels types, guided output, availability/language checks, budgeting, and error mapping.
- `SummaryWorker.swift`: durable-work actor, one in-flight generation task, wake scheduling, owner lock, cancellation, and bounded cleanup.

Modify `Models.swift` and `EngineClient.swift` for backwards-compatible snapshot/detail decoding and typed stdin requests. Modify `AppModel.swift`, `Views.swift`, and `TranscriberApp.swift` for worker lifetime, ID-based detail, settings, and synthetic preview export. Update `scripts/build_app.py` for the new Python source module and version defaults; update `README.md` for behavior and local build instructions.

Before Task 1, have Terra 6.1 xhigh capture the synthetic baseline with the existing debug `--preview --preview-panel --export-previews <directory>` exporter. Reuse size/theme after implementation. Preview runs must not touch personal runtime data.

### SQLite migration 1

Run `migrate_summaries(connection: sqlite3.Connection, now: float) -> None` from `Queue.__init__` after existing tables exist. Use one transaction, idempotent column inspection, and existing WAL/FULL policy. Do not migrate from `status` or detail reads. Keep summary schema version separate from prompt version and existing metadata.

```sql
ALTER TABLE jobs ADD COLUMN completed_at REAL;
CREATE INDEX IF NOT EXISTS summary_completed_jobs ON jobs(state, key);
CREATE INDEX IF NOT EXISTS summary_new_completed_jobs ON jobs(state, completed_at, key);
CREATE TABLE IF NOT EXISTS summaries (
    job_key TEXT PRIMARY KEY,
    source_hash TEXT,
    file_fingerprint TEXT,
    prompt_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','running','ready','failed')),
    bullets TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    attempt_token TEXT,
    owner_id TEXT,
    settings_revision INTEGER,
    priority INTEGER NOT NULL CHECK(priority IN (0,1)),
    queue_order INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS summary_due ON summaries(state, priority, queue_order);
CREATE INDEX IF NOT EXISTS summary_queue_order ON summaries(queue_order);
```

Acquire `BEGIN IMMEDIATE` before inspecting/migrating schema; guard `ALTER TABLE` with `PRAGMA table_info(jobs)`. Existing completed jobs retain NULL `completed_at`, identifying history. `Queue.complete` and successful `Queue.publish` stamp completion time without adding inference or file hashing. In `Queue.move`, preserve the winning job and its summary, delete losing summaries inside the existing merge transaction, and resolve incoming IDs through `aliases`. Never move a losing attempt onto the winner.

Insert missing metadata values only: `summary_schema_version="1"`, `summary_enabled="true"`, `summary_settings_revision="0"`, `summary_first_enabled_at=<now as JSON number>`, `summary_scan_cursor={"after_key":null,"upper_key":null}`. `summary_owner` stores a JSON UUID string after lock acquisition, initially absent. Prompt policy starts at integer `1`, authoritative in Python and checked against supported Swift policy `1`. No transcript bodies or intermediate model sessions in metadata.

`file_fingerprint` is canonical JSON containing resolved path, device, inode, size, `mtime_ns`, and `ctime_ns`. Compare stat before/after reading bytes; uncertain or changed fingerprints cannot validate a cache. Decode UTF-8 without newline conversion. SHA-256 hashes the full extracted body's UTF-8 bytes without whitespace normalization. Strip only the exact generated `Chat:/Sender:/Received:/Message:` metadata block and its separator; retain all body whitespace. Headerless files stay whole. Missing/unreadable files have null hash/fingerprint and a safe failure code. Reconciliation preserves unchanged failures; a changed readable source or prompt creates new pending work.

### JSON and Swift types

Keep envelope/event `schema_version:1`; the summary migration has its own version. Commands return the existing `{schema_version:1,ok:true,data:...}` envelope. Every request below is a single JSON object on stdin followed by EOF, never transcript argv. UTF-8 bodies use `collect`, never `EventDecoder`.

Wire keys use snake_case; Swift properties use camelCase with explicit coding keys where acronym conversion would be ambiguous. All wire structs are `Codable, Sendable`; summary/detail values are also `Equatable`. Nullability below is literal, not an omitted success response.

| Value | Exact fields and Swift types |
| --- | --- |
| `SummarySettings` | `enabled: Bool`, `revision: Int`, `first_enabled_at: Double?` |
| `SummaryRecord` | `state: SummaryState`, `bullets: [String]`, `error_code: String?`, `source_hash: String?`, `prompt_version: Int` |
| `SummaryState` | String enum `pending`, `running`, `ready`, `failed`; unknown decode becomes `pending`, with no bullets |
| `TranscriptDetail` | `id,title,timestamp,path: String`, `text: String?`, `source_hash: String?`, `summary: SummaryRecord` |
| `SummaryIdentity` | `owner,id,token,source_hash: String`, `prompt_version,settings_revision: Int` |
| `SummaryClaim` | `identity: SummaryIdentity`, `text: String`, `max_bullets: Int` |
| `SummaryScanResult` | `visited: Int`, `wrapped: Bool` |
| `SummaryOwnerResult` | `recovered: Int` |
| `SummaryClaimResult` | `claim: SummaryClaim?` |
| `SummaryCheckResult` | `valid: Bool` |
| `SummaryMutationResult` | `accepted: Bool` |

Add `summary: SummaryRecord` to `Transcript`, and `summary_settings: SummarySettings`, `summary_counts: [String:Int]` to `EngineSnapshot`. Summary counts include total/ready/pending/running/failed and default to an empty map for older snapshots; use these for compact backfill progress in Settings. Older snapshots decode missing settings as enabled=true/revision=0/firstEnabledAt=nil and missing summary as pending/[]/null/null/promptVersion=1. Update `.empty`, `.preview`, and existing initializers. Old transcript `text` remains a preview; it is never the full-text source. Clear bullets whenever state is not ready or its fingerprint/prompt validation fails. Disabled/unavailable are presentation statuses, not durable `SummaryState` cases.

`SummaryStore(database: Path, output_root: Path)` in Python exposes `handle(command: str, request: dict) -> dict`, with `close()` for deterministic connection cleanup. EngineClient implements the testable `SummaryStoreClient: Sendable` protocol with these async throwing methods. Omit default arguments in protocol requirements; concrete EngineClient methods provide the defaults shown. Put new Swift tests in suites named `SummaryContract`, `SummaryGeneration`, `SummaryWorker`, and `SummaryPresentation` so the commands below select real tests.

| CLI command | Request | Swift method and returned data |
| --- | --- | --- |
| `transcript` | `{id:String}` | `transcript(id: String) -> TranscriptDetail` |
| `summary-status` | `{id:String}` | `summaryStatus(id: String) -> SummaryRecord` |
| `summary-settings` | `{}` reads; `{enabled:Bool}` writes | `summarySettings(enabled: Bool? = nil) -> SummarySettings` |
| `summary-owner` | `{owner:String}` | `summaryOwner(owner: String) -> SummaryOwnerResult` |
| `summary-reconcile` | `{owner:String,id:String?}` | `summaryReconcile(owner: String, id: String? = nil) -> SummaryScanResult` |
| `summary-claim` | `{owner:String}` | `summaryClaim(owner: String) -> SummaryClaimResult` |
| `summary-check` | `{identity:SummaryIdentity}` | `summaryCheck(identity: SummaryIdentity) -> SummaryCheckResult` |
| `summary-finish` | `{identity:SummaryIdentity,outcome:"ready"|"failed"|"interrupted",bullets:[String],error_code:String?}` | `summaryFinish(identity: SummaryIdentity, outcome: SummaryOutcome, bullets: [String], errorCode: String?) -> SummaryMutationResult` |
| `summary-retry` | `{id:String}` | `summaryRetry(id: String) -> SummaryMutationResult` |

`SummaryOutcome` is the three-case raw String enum above. Rejected/stale operations return accepted=false, not an app-wide error. Invalid requests and absent/non-complete transcript IDs return safe envelope errors. An existing completed job with an unreadable original returns detail with text=null and a safe failed projection. `summary-status` validates stored fingerprint/prompt through stat only, never rereads bodies or copies the database; a changed/uncertain fingerprint returns pending with no bullets/source_hash until reconciliation validates it. Python never returns OS exception paths or transcript content in error messages.

Safe note error codes: `original_missing`, `original_unreadable`, `unsupported_language`, `refused`, `invalid_output`, `context_limit`, `generation_failed`. Validate bullets in Swift and again in Python: array of nonempty strings, exactly one for <=280 Python Unicode characters/Swift Unicode scalars, otherwise 1...3. Keep failed output empty. Runtime unavailable reasons are `requires_macos_26`, `device_not_eligible`, `apple_intelligence_not_enabled`, `model_not_ready`, `unknown`; they do not become permanent note failures.

## Tasks

### Task 1: Migrate persistence and read complete originals safely

**Files:** Create `app_summaries.py`, `tests/test_summaries.py`; modify `transcriber.py`, `app_state.py`, `tests/test_app_engine.py`, `tests/test_transcriber.py`.

**Interfaces:** Produce migration and `SummaryStore` above, plus `read_transcript_body(output: str, output_root: Path) -> tuple[str, str, dict]` and `project_summary(db: sqlite3.Connection, job_key: str, output: str, output_root: Path) -> dict`. Projection reads stat and stored hash/fingerprint only. It must work against unmigrated databases without creating files or tables. Read commands require an existing database and use mode=ro, query_only, an explicit read transaction, and NO_CKPT_ON_CLOSE where supported, with no migration or SQL mutations. SQLite may create/update coordination sidecars; readers must not change DB/WAL contents. Retain queue_snapshot for the stronger existing app-status source-file guarantees. Never use immutable reads for a live WAL database or copy the whole DB per summary. Mutable commands use short transactions on the same database, not a new store.

- [ ] Add failing tests `test_migration_preserves_jobs_aliases_and_originals`, `test_full_body_preserves_whitespace_and_headerless_paragraphs`, `test_replacement_and_symlink_escape_hide_cached_summary`, `test_snapshot_never_hashes_or_writes`, and `test_merge_discards_losing_summary_attempt`. Use real SQLite and temporary files. Include >65,536 characters, >1 MiB, emoji, repeated blank lines, missing originals, and unchanged file size with preserved mtime but changed inode/ctime. Assertions include:

  ```python
  self.assertEqual(body, expected_complete_body)
  self.assertEqual(source_hash, hashlib.sha256(body.encode("utf-8")).hexdigest())
  self.assertEqual(original.read_bytes(), before_bytes)
  self.assertEqual(projected["bullets"], [])
  self.assertEqual(queue.target(alias), winner)
  ```

- [ ] Run `python3 -m unittest discover -s tests -p 'test_summaries.py' -v`; confirm failures describe missing behavior.
- [ ] Implement migration, exact header extraction, validated `.txt` containment under configured output, stable fingerprint/hash, read-only projection, completion stamps, and transactional merge cleanup. Preserve existing snapshot source-file/DB/WAL read-only guarantees; update synthetic header fixtures to match real generated metadata. Do not hash full originals in snapshots.
- [ ] Run the focused command and `python3 -m unittest discover -s tests -p 'test_app_engine.py' -v` and `python3 -m unittest discover -s tests -p 'test_transcriber.py' -v`; all pass. Fresh unconfigured status still creates nothing.

### Task 2: Add durable scheduling and stdin contracts

**Files:** Modify `app_summaries.py`, `app_engine.py`, `app_state.py`, `NativeApp/Sources/TranscriberCore/Models.swift`, `NativeApp/Sources/TranscriberCore/EngineClient.swift`; create `NativeApp/Sources/TranscriberCore/SummaryTypes.swift`, `NativeApp/Tests/TranscriberCoreTests/SummaryContractTests.swift`; extend `tests/test_summaries.py` and `NativeApp/Tests/TranscriberCoreTests/ProcessTests.swift`.

**Interfaces:** Produce all CLI/Swift contracts above. Extend `EngineProcess.collect(payload: Data? = nil, timeout: TimeInterval = 30) -> Data` and private typed request encoding. Start stdout/stderr readers before potentially large stdin writes; close stdin after the one request. Include writing/reading in the request deadline so blocked stdin cannot bypass cancellation/timeout. Preserve streaming parent supervision. Do not add a body-size cap to request collection.

- [ ] Add failing durable tests for 61 completed jobs across 25-item pages, process recreation between pages, completion behind cursor, source/prompt replacement, failure preservation/manual Retry, two competing claims, stale owners, Off/On stale finishes, and alias invalidation. Add cross-language fixture decode tests for every response shape and old snapshots, plus a real subprocess large-stdin/stdout exchange beyond 1 MiB. Assert:

  ```python
  self.assertEqual(set(discovered_ids), set(all_completed_ids))
  self.assertEqual(claimed_ids, expected_new_fifo + expected_historical_fifo)
  self.assertFalse(stale_finish["accepted"])
  self.assertEqual(settings_after_off["revision"], settings_before["revision"] + 1)
  ```

- [ ] Run focused Python tests and `swift test --package-path NativeApp --build-system native -j 4 --filter SummaryContract`; confirm the new behavior fails before implementation.
- [ ] Implement each command. Reconciliation first discovers missing summary rows for new completions ordered by completed_at/key using the new-completion index, then spends the remaining 25-item budget on the persisted historical sweep. This also catches new notes completed while Off before any historical claim. The sweep uses keyset pages bounded by captured `MAX(key)`, persists after_key/upper_key, resets at range end, then wraps. New-note ID hints trigger ordered completion discovery, rather than enqueueing that ID ahead of earlier undiscovered completions. A historical ID hint may reconcile that job without moving the sweep cursor. Claim priority-0 rows by completed_at/key, then priority-1 rows by queue_order, so reversed hints cannot reorder new completions. Hash changed sources outside write transactions; recheck canonical job/output/fingerprint before committing. Remove orphans with an indexed anti-join. Read commands never reconcile.
- [ ] Assign priority 0 to `completed_at >= summary_first_enabled_at`, otherwise 1. Allocate `queue_order=COALESCE(MAX(queue_order),0)+1` inside the enqueue transaction, retain it on interruption, renew it on explicit retry or changed source/prompt. Claim by priority, then actual completed_at/key for priority 0, then queue_order for priority 1. Before claiming, reconcile missing new completions in completion order; never claim a later discovered completion ahead of an earlier undiscovered one. Reconcile one page between claims and process new-completion hints first so backfill cannot hold the queue until its whole scan finishes.
- [ ] Implement owner/CAS semantics. Only the Swift process holding `summary-worker.lock` registers a fresh owner UUID. Registration atomically replaces metadata owner, returns running rows to pending, clears tokens, and preserves queue order. Claim atomically persists running/owner/token/revision after outside-transaction full-body validation and transactional rechecks. Check/finish require canonical completed job, current owner/token/hash/prompt, enabled=true and matching revision. Finish rehashes full body outside the transaction, verifies stable fingerprint and current job/output again, then commits the compare-and-set. A hash change queues replacement work instead. `interrupted` requeues without a failure; an obsolete token cannot alter a newer attempt.
- [ ] A changed enabled value increments revision in the same transaction. Off invalidates every running token and returns those rows to pending immediately; ready rows remain. On schedules catch-up without resetting unchanged failures. Retry requires enabled=true and a failed canonical job, creates pending work with no token; model availability is a Swift precondition. Repeat focused Python/Swift tests and `swift test --package-path NativeApp --build-system native -j 4 --filter largeSummaryPayloadRoundTrip`, the new process test's exact name; all pass.

### Task 3: Implement isolated native generation and complete long-note coverage

**Files:** Create `NativeApp/Sources/TranscriberCore/SummaryGenerator.swift`, `NativeApp/Sources/TranscriberCore/FoundationSummaryModel.swift`, `NativeApp/Tests/TranscriberCoreTests/SummaryGenerationTests.swift`; extend `SummaryTypes.swift`; modify `NativeApp/Package.swift` only for weak linking if required by the compiled binary.

**Interfaces:** Define `SummaryAvailability` as `.available` or `.unavailable(SummaryUnavailableReason)`, whose raw String enum cases are the runtime reason codes above, with camelCase Swift names. Define `SummaryModelRequest { text: String, languageCode: String?, stage: SummaryStage }`, where `SummaryStage` is `.facts` or `.final(maxBullets: Int)`. `SummaryModel: Sendable` supplies `availability() async -> SummaryAvailability`, `languageCode(for body: String) async throws -> String?`, `fits(_ request: SummaryModelRequest) async throws -> Bool`, and `respond(_ request: SummaryModelRequest) async throws -> [String]`. `SummaryGenerator(model: any SummaryModel)` supplies `generate(body: String, maxBullets: Int, checkpoint: @escaping @Sendable () async throws -> Void) async throws -> [String]`. Typed `SummaryGenerationError` cases are `.interrupted`, `.unavailable(SummaryUnavailableReason)`, `.contextOverflow`, and `.note(code: String)`.

- [ ] Add fake-model tests for 280/281 scalar boundaries, combining sequences and emoji, English/French/mixed inputs, all chunk ranges concatenating to the exact original, tail facts entering reduction, oversized sentences, context-overflow splitting, cancellation between requests, refusal/unsupported language/invalid output, and a reducer that refuses to shrink. Assert byte-for-byte input coverage, unchanged dominant languageCode through every stage, final count constraints, maximum concurrent respond calls=1, no partial result after failure, and bounded request count for a nonshrinking reducer.
- [ ] Run `swift test --package-path NativeApp --build-system native -j 4 --filter SummaryGeneration`; confirm new logic fails, then implement the pipeline. Detect dominant language once from the complete original and retain it through all requests. Prefer sentence boundaries, split oversized sentences on Unicode boundaries, and never trim/drop source spans. Each facts request produces 1...8 nonempty strings; ordered reduction groups carry every child. Use 512 reserved output tokens plus a 256-token safety margin per request. On 26.0–26.3 start with <=2,048 input UTF-8 bytes, a heuristic only. Halve overflowing inputs with at most 12 splits per span; fail at an unsplittable scalar. Require each reduction level to shrink total UTF-8 bytes, cap at 16 levels, and fail with `context_limit` rather than loop or silently truncate. These are internal safety constants, not settings.
- [ ] Implement `FoundationSummaryModel` as an availability-gated actor using only `SystemLanguageModel.default`, fresh `LanguageModelSession` per respond call, default guardrails, no tools, and guided bullet-array output. Treat source and intermediate facts as quoted data. Require output in the original's dominant language and preserve mixed-language terms. Use NaturalLanguage dominant-language detection against model-supported language/locale data, not the Mac locale; unknown language may attempt generation, with SDK unsupported-language errors mapped safely.
- [ ] On macOS 26.4+, count actual instructions, guided schema, stage prompt, input, reserved output, and margin against `contextSize`; budget facts and every reduction as well as final output. On earlier supported versions use the heuristic and context-overflow recovery. Checkpoint before every native request, including retries and reductions. Map cancellation/assets unavailable to interruptions, note-specific refusals/language/output failures to safe codes. Do not save intermediate or partial output.
- [ ] Keep every FoundationModels import/type/macro declaration inside `FoundationSummaryModel.swift`, gated with `#if canImport(FoundationModels)` and `@available(macOS 26.0, *)`. A factory returning `any SummaryModel` uses `if #available` and a framework-free unavailable implementation on macOS 15. No FoundationModels types in ungated stored properties, protocol signatures, global initialization, or view models. Run focused tests and `swift build --package-path NativeApp --build-system native --triple arm64-apple-macosx15.0 -j 4`; both pass. Task 6 audits weak load commands and documents any unavailable older-OS runtime check.

### Task 4: Own one worker through settings changes, interruption, and quit

**Files:** Create `NativeApp/Sources/TranscriberCore/SummaryWorker.swift`, `NativeApp/Tests/TranscriberCoreTests/SummaryWorkerTests.swift`; modify `SummaryTypes.swift`, `AppModel.swift`, `TranscriberApp.swift`.

**Interfaces:** `SummaryWorker(store: any SummaryStoreClient, model: any SummaryModel, dataRoot: URL, onUpdate: @escaping @Sendable (SummaryWorkerUpdate) async -> Void)` exposes `start() async`, `wake(transcriptID: String? = nil) async`, `settingsChanged() async`, `refreshAvailability() async -> SummaryAvailability`, and `stop(cleanupLimit: Duration = .seconds(2)) async`. `SummaryWorkerUpdate: Sendable` has `.availability(SummaryAvailability)` and `.completed(id: String)` cases, no text. `SummaryWorkerLock` owns a nonblocking exclusive `flock` descriptor with close-on-exec, outside SQLite.

- [ ] Add fake-store/model tests for priority/FIFO, two actual worker lock contenders, crash/owner recovery, unavailable/resume, panel closure and capture pause, cancellation followed by ignored late completion, Off/On across two clients, and shutdown with a model that never settles. Assert:

  ```swift
  #expect(maximumConcurrentRequests == 1)
  #expect(staleResultWasPublished == false)
  #expect(secondOwnerAcquiredWhileRequestUnsettled == false)
  #expect(summaryCleanupElapsed < .seconds(2.5))
  ```

- [ ] Run `swift test --package-path NativeApp --build-system native -j 4 --filter SummaryWorker`; confirm failures, then implement one actor-driven work loop at utility priority, separate from MainActor and `AppModel.isBusy`. Lock once per data root, then register/recover. Never reset claims before acquiring ownership. Busy lock means observe only and retry ownership on later wake, not a competing generation loop.
- [ ] Keep exactly one tracked generation task even after cancellation is requested. In-flight task completion clears the slot; Off/On cannot clear it early. Before each chunk/reduction request, call summary-check and recheck availability/local epoch. Observe successful settings writes across app instances with a distributed notification named using a hash of canonical data-root path, no message contents; notify only after persistence. While inference is active, poll settings at one-second intervals as a missed-notification fallback. Off persists before cancellation, invalidates local epoch, and requests task cancellation. No native request starts again until the old request actually settles.
- [ ] Start after database initialization, independently of capture readiness and panel visibility. New transcript events enqueue an ID hint after ordinary notification dispatch; summaries never delay notifications. Recheck at launch, wake, app activation, and existing 20-second closed-panel refresh even when capture setup is unavailable. Do not gate this on `AppModel.isBusy`. Run bounded page reconciliation while pending work exists, and periodic wrapped scans when idle. Off prevents claims. Availability loss interrupts current work, preserves On intention, and resumes without manual Retry when availability returns. A failed note does not block later notes.
- [ ] Reshape `AppModel.shutdown()` to invalidate local completion epoch immediately, cancel monitor/startup/operations, request summary cancellation, and stop capture/pairing concurrently. Bound summary persistence/cleanup to two seconds using an unstructured task plus a completion signal/deadline; do not race native generation inside a structured task group whose scope still awaits it. Await capture cleanup before `client.cancelAll()`. Keep the summary lock until native work settles or process exit, even if cleanup times out. Token CAS rejects late callbacks; next owner recovers a stranded running claim. Repeat focused tests and existing process-lifecycle tests; all pass.

### Task 5: Show summaries and retain full originals

**Files:** Modify `AppModel.swift`, `Views.swift`, `TranscriberApp.swift`, `Models.swift`, `SummaryTypes.swift`; create `NativeApp/Tests/TranscriberCoreTests/SummaryPresentationTests.swift`.

**Interfaces:** Change `PanelRoute.detail(Transcript)` to `.detail(String)`. AppModel owns current `TranscriptDetail?`, its loading/error state, availability, summary settings, and visit-scoped presentation state. Every open, including reopening the same ID, creates a fresh UUID visit token. Async detail/status callbacks apply only when their request ID and visit token still match. Keep Show original selection in AppModel-owned visit state, not a recreated SwiftUI root view. `openTranscript(id: String)` fetches full detail independently of inference. `copyTranscript(id: String)` fetches full body and writes pasteboard only after success; null/error does not replace clipboard with preview text. `setAutomaticSummaries(_ enabled: Bool)` persists immediately through the engine, then notifies/wakes the worker. `retrySummary(id: String)` requires On and available model.

- [ ] Add presentation-state tests for old snapshots, ready/pending/running/failed/disabled/unavailable views, saved ready summaries while Off, current detail updating by ID, explicit original selection surviving completion, selection resetting on a new visit, out-of-order detail/status replies across A/B/A visits, root-view recreation preserving original selection, and copy returning complete body beyond both prior caps. Assert:

  ```swift
  #expect(selectionAfterSummaryRefresh == .original)
  #expect(selectionOnNextVisit == .summary)
  #expect(copyPayload == completeOriginalBody)
  #expect(copyPayload != summaryBullets.joined(separator: "\n"))
  ```

- [ ] Run `swift test --package-path NativeApp --build-system native -j 4 --filter SummaryPresentation`; confirm failures, then implement the small visit-selection value in `SummaryTypes.swift` and wire views. Inbox labels ready bullets `Summary`. Detail defaults to valid summary, offers `Show original`/`Show summary`, and retains text selection. Without a valid summary, show full original plus concise status; failed originals show a safe explanation. Retry appears for a failed note and is disabled unless enabled/available. Unknown availability gets a useful fallback explanation. Refresh selected summary by ID with `summary-status` on worker completion and normal refresh without resetting selection or rereading full originals. Fetch full detail once per visit, for copy, and when a newly validated source hash differs from the loaded body's hash. Test that unchanged periodic refreshes perform zero full-body reads.
- [ ] Add `Automatic summaries` switch with copy explaining saved notes are included and Apple Intelligence runs on this Mac. Availability reason is visible without turning Off intention off. Keep summary settings independent of capture busy state. Do not promise accuracy; retain generated label and original access. Add synthetic preview fixtures for ready, pending, failed, unavailable, Off-with-ready-cache, and long French detail.
- [ ] Repeat focused tests. Have Terra 6.1 xhigh capture after implementation against the saved baseline. Extend exporter route setup for ID-based details and new states. All captures use synthetic data and isolated runs; updater/pairing/capture stay inactive. Deliver paired inbox, detail, original, and Settings screenshots; capture a short flow video if native tooling works. Previous native CUA UI calls timed out: report a repeated failure promptly and use the existing exporter for stills, never claim unobserved click/video validation.

### Task 6: Verify local packaging and independent review

**Files:** Modify `scripts/build_app.py`, `README.md`; use existing tests and generated `.build/` or `dist/qa-1.2.0/` artifacts. No new release feed or published artifact.

**Interfaces:** Local builder defaults and explicit invocation use `1.2.0`/`3`. Add `app_summaries.py` explicitly to the source-material copy tuple in `sources()`. Its static engine import must include it in PyInstaller's frozen engine. Preserve existing signing, Sparkle settings, deployment audit, and source sanitization.

- [ ] Update module source archive list and documentation. Run `python3 -m unittest discover -s tests -v` and `swift test --package-path NativeApp --build-system native -j 4`; expect all meaningful suites green. Do not add tests that merely search packaging source for filenames.
- [ ] Build local QA only: `python3 scripts/build_app.py --version 1.2.0 --build-number 3 --output-dir dist/qa-1.2.0`. Inspect built Info.plist and archived module contents; run the frozen engine's initialize/status/transcript/summary operations against synthetic temporary data. Exercise the packaged engine beyond 1 MiB and compare copied body bytes. Inspect `otool -l dist/qa-1.2.0/Transcriber.app/Contents/MacOS/Transcriber` for minimum OS 15 and weak FoundationModels loading, using an explicit weak framework linker setting if needed. Build success is not proof of macOS 15 execution; record actual older-OS test availability.
- [ ] Delegate nonimplementation measurement to Sol 6.1 high, read-only: generate a synthetic realistic library, measure existing snapshot DB-copy cost and repeated stat-only cache reads at increasing sizes, then inspect query plans for indexed pending/keyset work. Record median timings and library size; no per-note DB copy, whole-library body loading, repeated full-file hashing on snapshots, or inference inside transactions. Parent fixes material regressions.
- [ ] Delegate native QA to Terra 6.1 xhigh with full access and instruction `do not edit files`. Check model availability without reading personal runtime data. If available, use synthetic English/French short and long notes for real-model smoke, including names/dates/amounts, final tail facts, dominant language, and injected transcript instructions. Check offline generation after assets are available, unsupported-language handling where reproducible, Off/On, original-copy integrity, and quit. Use synthetic isolated data root; never the personal WhatsApp store. Do not wait indefinitely for asset downloads. Record availability and any blocked cases. No exact wording assertions, cloud/model tools, feedback uploads, or simulators.
- [ ] Obtain fresh Sol 6.1 high independent bug/performance review, full access, `do not edit files`. Focus on cross-language fields/defaults, hash/stat races, alias merges, owner lock inheritance/lifetime, Off revision across instances, single native request through cancellation, reduction coverage/termination, nonblocking capture, shutdown ordering, weak linking, and archive completeness. Parent fixes findings and reruns only affected checks plus required final suites. This plan itself gets a fresh Sol review before Task 1.
- [ ] Self-check diff for scope, no comments/personal data, and unchanged v1.1.0/feed assets. Parent follows repository integration policy and commits the completed single feature after review; this planning subtask makes no commit. Report tests, before/after visuals, model availability, and remaining QA limits. Next step after local QA is user review of release readiness; publication still needs authorization.

## Self-review and known limits

Coverage checked against every design section: Tasks 1–2 own complete originals/cache/migration/queue identity; Task 3 owns local language-aware generation and full-text budgeting; Task 4 owns cross-instance settings/lifetime; Task 5 owns summary-first UI and original copy; Task 6 owns packaging, performance, independent review, real-model and visual QA. Wire names, nullability, state enums, and identity fields above are shared across tasks.

Remaining external risks: actual model availability and cancellation responsiveness; older-macOS runtime access; native CUA timeouts; lossy hierarchical compression. Fake-model tests must cover unavailable/uncooperative behavior even when live checks cannot. The lock stays held for uncooperative generation, so the app may intentionally defer work until process exit. No remaining product-approval question.

## Review rulings

Sol 6.1 high plan review completed before implementation. Reversed new-note hints now preserve completion order. AppModel-owned visit UUIDs reject stale A/B/A detail responses and retain original selection through view recreation. Targeted SQLite probes confirmed ordinary mode=ro reads can update shared-memory coordination; new commands permit those sidecars while forbidding SQL mutations and DB/WAL content changes. Existing app-status snapshot purity stays unchanged. Strict no-sidecar concurrent reads would require copying or a custom VFS and is not part of this feature.
