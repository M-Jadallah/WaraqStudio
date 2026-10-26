# Waraq embedded account engine

The offline rotation core and threshold settings are implemented but remain
disconnected from this live RPC. API version remains **1**. There is no
`rotation.configure`, `rotation.status` or `accounts.reorder` RPC in this release.
Activation stays blocked pending the official-source gate. See
[`docs/ACCOUNT_ROTATION_CORE.md`](../docs/ACCOUNT_ROTATION_CORE.md) for the tested
internal contracts and remaining integration work.

Source: [ndycode/oc-codex-multi-auth](https://github.com/ndycode/oc-codex-multi-auth), release 6.29.0, commit `f0d251b03b4f9b77c730be93e510cf8efd86c252`, MIT. `vendor/LICENSE` is retained. The fork targets OpenCode and its plugin SDK **2.0.32**, packaged Node **24.22.0**, Windows x64. The desktop uses the headless adapter, not the upstream TUI.

`vendor/` preserves upstream source. Waraq patches the filesystem boundary to DPAPI, fixes private global storage, removes host/native-Codex credential hydration, disables updates/admin tools/model fallback/credit spending, and binds each model location to one account with AsyncLocalStorage. The host's module reload graphs share private in-process state so concurrent task locations cannot create separate pools or bypass concurrency limits. All package dependencies are locked with npm integrity hashes. Installing/running Waraq never invokes npm.

OpenCode receives the plugin **directory**, containing root `index.js`. The plugin captures a startup-only capability and erases it from its environment. Python communicates over authenticated loopback HTTP:

`POST /api/rpc/waraq-accounts/{method}?location[directory]=<absolute-directory>`

Body: `{"input":{"apiVersion":1,"capability":"<startup-secret>",...fields}}`.

Native response: `{"output":{"ok":true,"data":{...}}}` or `{"output":{"ok":false,"error":{"code":"FIXED_CODE"}}}`. No credential values are returned. Public native timestamps are milliseconds; the Python facade normalizes them to Unix seconds. Window duration, rather than primary/secondary order, determines five-hour/weekly labels.

| Method | Additional fields | Result |
| --- | --- | --- |
| `health` | none | API version, plugin version, storage, browser/device catalog |
| `accounts.list` | none | allowlisted account metadata and quota snapshots |
| `account.create` | alias | staged opaque account reference |
| `account.update` | accountRef, optional alias/enabled/priority | account metadata |
| `account.prefer`, `account.remove` | accountRef | preference/removal acknowledgement |
| `auth.begin` | accountRef, method browser/device | attemptId, browser URL or device code |
| `auth.status`, `auth.cancel` | attemptId | fixed authentication state |
| `usage.refresh` | optional accountRef, manual | remote quota snapshot; coalesced per account, two readers maximum |
| `attempt.begin` | attemptId, jobId, workerId, model, directory, maxConcurrency | immutable location/account binding |
| `attempt.resolve` | attemptId | binding state, sanitized provider failure and events |
| `attempt.close` | attemptId, outcome | closed binding; only after native inactivity proof |

Account lifecycle mutations reject busy/authorizing identities. Pending OAuth completion validates the opaque identity binding and rejects duplicates. Reset/reauth information comes from provider responses, not model text. A failed provider request blocks that attempt from silently reusing or replacing its account. Administrative RPC methods are not model-visible tools.

`tests/verify_embedded_engine.py` runs a packaged host and actual plugin with synthetic OAuth-shaped data. A test-only fetch wrapper redirects provider traffic to localhost and rejects every other external destination. This tests the local integration, not live OAuth availability or a production subscription. `engine/test/boundaries.test.mjs` verifies DPAPI payloads above Windows Credential Manager's blob limit and concurrent account pins.
