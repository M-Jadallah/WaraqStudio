# Architecture

The native Qt Quick/QML UI talks to a QObject bridge. The bridge runs source discovery off the GUI thread and presents snapshots of durable state. SQLite WAL transactions serialize task claims. A scheduler owns one download slot plus independent Deepgram and OpenCode worker pools. Each worker has at most one active job and a private cancellation event.

Modules:

- `config.py`: validated non-secret settings, atomic writes, Windows credential-store integration and explicit legacy import.
- `store.py`: tasks, stages, events and metadata cache; atomic claims and crash recovery.
- `services.py`: serialized YouTube access, speech normalization, Deepgram response/chunk persistence, isolated OpenCode execution, DOCX validation.
- `scheduler.py`: dependency/order gates, per-project concurrency, downstream queue/disk backpressure and worker lifecycle.
- `bridge.py`: UI operations and background discovery; no paid requests on startup.
- `qml/`: native screens and reusable controls.

Task stage states: pending → running → completed. Terminal/action states: failed, blocked, uncertain, cancelled. Paused is used for local operations interrupted by process exit. Skipped stages count as dependency-satisfied only for valid plans. Claims compare-and-set `pending` under a database lock so two workers cannot execute the same stage.

Settings are snapshotted at enqueue. Worker model/language/agent parameters follow that snapshot; repaired credentials and executable/cookie paths apply to waiting jobs. Pool membership and worker enabled state apply immediately to future dispatch. Explicit reprocessing creates a new revision fingerprint and retains prior artifacts.

Deepgram commits raw responses before parsing and before honoring cancellation. Chunk responses are individually durable; a manifest marks completed audio segmentation. Lost network responses are uncertain rather than blindly retried. This reduces duplicate billing but cannot provide exactly-once external execution.

OpenCode works in a separate directory per task revision that receives a copy of the transcript; the prompt carries that copy's path plus the exact target DOCX path. The original TXT stays outside the agent's reach, and its hash is still verified after the run. Success requires process exit success, a valid editable DOCX, and an unchanged original hash. This is isolation of working files, not an OS security sandbox; OpenCode retains the account permissions granted by its own configuration, and a rejected external-directory permission is reported as a dedicated failure.

Known product limits are documented in README_AR.md. Native integration and scheduler tests use temporary directories and service doubles; external provider availability and actual formatting require live acceptance testing.
