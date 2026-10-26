# Offline account rotation core

The user authorized implementing the core, settings and tests while keeping live activation blocked. This changes the development scope after the initial feasibility report; it does **not** turn the failed source gate into a pass.

## Delivery boundary

Implemented:

- `engine/src/rotation-policy.js`: strict remaining-percentage policy, exact required windows, 60-second freshness, source/account/scope binding and explicit circular ordering.
- `engine/src/quota-provider.js`: unavailable production adapter, injected synthetic-reader support, allowlisted snapshots, one read per account, two concurrent readers globally, abort deadlines, generation invalidation and bounded retry backoff respecting `retryAfterMs`.
- `engine/src/rotation-coordinator.js`: serialized durable transitions, one active identity, revisioned configuration/reordering, drain barriers, proof-based owner release, restart holds, recovery confirmation and replay eligibility.
- `engine/src/request-gate.js`: an unconnected request adapter that retains permits until response-body completion, including terminal SSE detection and uncertainty on truncation/cancellation/transport loss.
- `engine/src/replay-safety.js`: explicit trusted tool classification; unknown or unfinished effects cannot be replayed.
- Python defaults/validation and native settings UI: save a finite threshold in `[0,100]`, default 20, with zero/decimal support and an unavailable activation checkbox. Activation through raw settings is also rejected. Hand-edited activation is repaired off at load.

Not connected:

- No approved production quota reader, OAuth change, paid probe or external token injection.
- No `rotation.*` RPC methods and no RPC version bump. Existing accounts still use RPC v1.
- No live request/tool-hook installation, scheduler routing change, SQLite run-intent migration or automatic application-level resumption.
- No replacement of the existing account registry, credential protection, execution holds or Word commit logic.

The default coordinator cannot activate. A quota payload, settings entry or offline schema receipt cannot grant source verification. The generic reader constructor accepts verification only as a trusted adapter dependency for deterministic tests; it is not exposed through RPC/settings. Any future production adapter requires the documented source review and package validation first.

## Contracts

All core timestamps are Unix **milliseconds**. Python/UI timestamps elsewhere remain governed by their existing contracts.

A sanitized account contains `accountRef`, `bindingVersion`, `enabled`, `loginState`, `providerEligible`, and optional `blockedUntil`. The future native adapter must compute provider eligibility and supply the exact scope; the core never guesses model entitlement or treats account login as quota recovery.

A reader receives `{accountRef, bindingVersion, scope, signal}` and returns an allowlisted snapshot:

```js
{
  accountRef, bindingVersion, scope, observedAt,
  status: 'available', coverageComplete: true,
  ordinaryUsageAllowed: true,
  requiredWindowIds: ['five_hour', 'weekly'],
  windows: [
    { id: 'five_hour', remainingPercent: 80, windowMinutes: 300, resetsAt },
    { id: 'weekly', remainingPercent: 60, windowMinutes: 10080, resetsAt }
  ]
}
```

These window IDs are fixture examples, not assumptions about every real plan. The approved adapter must establish the applicable windows and model/meter mapping. Null permission, missing required windows, stale/future observations, expired reset timestamps, wrong identities, malformed percentages or incomplete scope coverage cannot enable an account. Percentages are never rounded before comparison.

Readers must honor `AbortSignal`; the default deadline is eight seconds. A broken reader that ignores cancellation retains its slot until it settles, preventing additional upstream requests from exceeding concurrency. This does not certify such a reader for use. A failed read keeps previous values marked unavailable; it does not refresh their successful observation time. Rebinding invalidates pending results. Closing aborts active reads and rejects queued work.

`RotationCoordinator` requires an asynchronous `persist(nextState)` callback. A transition is not published or admitted before persistence succeeds. Storage failure latches `needs_action/storage_unavailable`; no later request is admitted by that instance. The callback must ultimately use the engine's existing encrypted/atomic storage boundary; tests use synthetic journals. No default plaintext writer or real account storage is added.

`proveStopped(owner)` defaults to false. The future adapter must resolve the attempt ID through authoritative execution ownership and verify native parent/child/tool inactivity. It must never accept a user-supplied proof flag. Proof runs outside the decision queue. Restart converts saved owners into recovery holds, discards cached eligibility/recovery confirmations and never recreates live leases. Reconciliation removes only proved-stopped holds.

Policy updates use `expectedRevision` and `operationId`; conflicting reuse is rejected. The last 64 configuration receipts and 256 completed attempt IDs are retained. These bounded receipts protect local retry episodes; permanent job/result deduplication remains the responsibility of the existing SQLite and validated-output records.

Admission tokens are unique and complete exactly once. Account failover waits for all owned attempts to close, not only for HTTP headers or request counters. An uncertain response or tool effect stays ineligible for replay even after native stop is proved. `ReplaySafety` classifications must originate in a trusted tool adapter; shell commands are not automatically classified as safe from their names or arguments.

`gatedRequest` must wrap **every actual send**, including retries and child sessions, when integrated later. It returns a wrapped response body; the consumer must consume or cancel it. SSE EOF without a terminal response event is uncertain. A terminal event ends the transport accounting, but does not establish a valid Word result. `services.py` must still validate the DOCX and original/copied transcript hashes before committing.

The core computes `nextCheckAt`; it does not start timers or network requests on construction. The future scheduler adapter must coalesce safety/display reads, wake on state changes, persist user run intent and honor manual pauses. Those integration steps remain blocked.

## Verification

Run the independent core tests with the packaged Node binary:

```powershell
& .\build\embedded-runtime\node\node.exe --test engine/test/rotation.test.mjs
```

The existing `npm test` glob also includes these tests during the pinned runtime build, before source/test pruning. No new package dependency is required.

Python and native settings checks:

```powershell
python -m pytest tests/test_account_rotation_settings.py tests/test_usage_config.py tests/test_account_bridge.py tests/test_ui_contract.py -q --tb=short
$env:QT_QPA_PLATFORM='offscreen'
$env:QT_QUICK_BACKEND='software'
$env:QT_QPA_FONTDIR='C:\Windows\Fonts'
python tests/verify_accounts_ui.py .verify/account-rotation-ui
```

The UI check validates saved zero, fractional and 100% values, disabled activation, the 100% explanation, real scale changes and scroll stability. Optional screenshots use synthetic rows at 1060×720, both themes and 100%/140% scale. The protected UI contract retains all previous actions/functions; only the three authorized settings/Bridge hashes are updated with their previous values recorded.
