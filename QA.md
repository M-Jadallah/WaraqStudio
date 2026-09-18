# Verification — 2026-09-17 · تحديث المزودين والمكتبة

## Local automated tests

51 engine/service tests passed, covering:

- Atomic task claims under competing workers.
- Crash recovery and uncertain paid requests.
- Original lesson numbers versus custom queue order.
- Stage dependencies and strict-order barriers.
- Independent API-key routing, five Deepgram and five OpenCode workers, one downloader.
- Shared-project concurrency cap, and five distinct project labels running in parallel.
- Raw Deepgram response recovery without another request.
- Per-chunk resume without retranscribing already saved chunks.
- Real FFmpeg WAV-to-MP3 conversion while retaining the local source.
- Literal subprocess arguments and cancellation of a real child process.
- Invalid/empty Word rejection and reuse only after committed validation.
- Revision creation while retaining prior files.
- Configuration validation and YouTube 429 cooldown.
- Bounded events log via startup and hourly pruning.
- DOCX packages size-capped before any decompression work.
- OpenCode runs on a transcript copy inside its revision workspace.
- OpenCode permission auto-rejections surface as a dedicated failure message.
- Transient download failures defer with a future `next_retry` and complete on retry; the budget caps attempts and success resets the counter.
- Deepgram failures are never auto-deferred (paid-stage protection); uncertain/blocked/cancelled stay terminal.
- OpenCode quota stops detected from the provider's streamed 429 event and deferred hourly up to the configured budget, then failed with a named message.
- Deferred retries persist across `recover()`; claims respect `next_retry`; `queue_drained()` is true only when nothing is pending or running.
- Prompt-template validation (required variables, unique ids, size cap) and enqueue-time template snapshotting into the job prompt.
- Default pools: five Deepgram workers with distinct projects and five OpenCode workers that all follow the user's own settings — no automatically created router workers.
- Pool migration: missing workers are cloned from the first saved one (keeping its model, reasoning level and provider), saved workers are never relabelled, and a worker the user deletes is never resurrected.
- Cleanup migration: the five OpenRouter workers an earlier release created are removed, the pool stays at five workers on the user's own settings, and a hand-made worker that merely uses a router provider is left alone.
- Provider validation: unknown provider on a worker, malformed provider id, id colliding with a built-in, malformed key variable, and non-boolean isolation are all rejected.
- Model-id composition per provider and the worker environment: isolated data/state directories plus the provider key for key-based workers, an untouched environment for login workers, and a user-defined provider fed the environment variable it declares.
- OpenCode command building: composed provider/model id, `--variant` dropped when empty, provider key present only for key-based workers.
- Key-based worker without a stored credential is parked with an explicit message while login workers keep running.
- A queue-level override rewrites provider, model and reasoning level for every OpenCode worker, reads the credential scoped to the forced provider, falls back to the worker's model when no forced model is given, restores each worker on clearing, and refuses an unknown provider.
- Completed stages mirror into the library; `library_auto` off leaves the mirror untouched.
- Library publish is idempotent, collision-safe for equal titles, refreshes a regenerated artifact, copies audio only when enabled, and deletion refuses paths outside the library folders.

Native UI interaction checks passed for all six screens, range selection, custom ordering, queue insertion, template publication/save/enqueue snapshotting, the auto-stop toggle wiring, direct worker enable toggles, provider management (adding one, rejecting a bad id and a reserved id, refusing to delete one in use), the pre-start override question starting the queue on both answers, library organise/list/delete, service pause/resume, theme persistence and a 1060 × 720 window. No QML errors were reported. Screenshots were inspected at 1380 × 900 and 1060 × 720 for Arabic shaping, reading order, contrast, spacing, modal readability and overflow. The interface is a native Qt application, so verification used Qt Test and QQuickWindow screenshots rather than a web browser.

The source environment resolves FFmpeg, yt-dlp, EJS, Requests, OpenPyXL, Windows Credential Manager, Node.js and the installed OpenCode executable.

## Limits of this verification

Service workers used deterministic test substitutes where network/paid providers would otherwise be called. No real YouTube download, Deepgram transcription or OpenCode document generation was performed for acceptance. DOCX tests check structure and editable text, not the visual fidelity of an actual lesson produced by the configured model.

Before a large batch, test one short video and inspect the Word result. YouTube restrictions and provider quotas remain external constraints. API keys and authentication data are not included in the release folder.
