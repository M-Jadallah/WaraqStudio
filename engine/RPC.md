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
| `accounts.export` | accountRefs, filePath, password | exported count; password-encrypted portable file |
| `accounts.import` | filePath, password | imported/skipped/total counts; existing identities retained |
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

Portable transfers are desktop-only administrative RPCs, excluded while any lease,
OAuth completion or quota request is active. A transfer blocks new quota requests;
the mutation queue serializes other account changes and attempt admission. Public
account metadata adds only `canExport`, indicating saved credentials exist.

`.waraq-accounts` v1 is a bounded JSON envelope (`format: WaraqStudioAccounts`,
`version: 1`, base64 `salt`, `nonce`, `tag`, `ciphertext`). It uses AES-256-GCM,
16-byte salt, 12-byte nonce, 16-byte tag and asynchronous scrypt
(`N=131072,r=8,p=1,maxmem=256 MiB`, 32-byte key). AAD is the UTF-8 bytes of
`WaraqStudioAccounts\0v1\0scrypt-131072-8-1\0aes-256-gcm\0` followed by the raw
salt and nonce. Algorithms and work factors are fixed by the format version.
The authenticated payload is `{version:1,accounts:[...]}` containing allowlisted
credentials, provider identity, alias and enabled state. All account details are
encrypted. Passwords are 12–256 Unicode code points, used verbatim as UTF-8.
They are neither stored nor logged; mutable key/plaintext buffers are cleared
after use (JavaScript strings remain subject to garbage collection).

Files are at most 10 MiB and account pools at most 20 entries. Imports authenticate
and validate the complete payload before one DPAPI-backed storage transaction.
Deduplication uses storage identity semantics on throwaway records and always
retains the existing account. Local opaque references, preferences, quotas and
live attempt state are not copied. Imported aliases/enabled state are durable in
the account pool and seed ordinary metadata reconstruction. Export writes only
encrypted bytes through the atomic writer and refuses paths inside private
storage, including junction aliases. Caller-selected files may be overwritten
after the desktop save dialog's normal confirmation.

Transfer failures use fixed `TRANSFER_*` codes for invalid password, failed
authentication, invalid format/version/selection/path, size/account limits, busy
state and I/O failure. Wrong passwords and authentication failures never write
account state; a successful import acknowledgment survives a later failed UI
refresh. These methods are not automatically retried after transport loss.
Their local RPC timeout is 180 seconds to allow bounded scrypt processing and
cold storage on slower Windows machines; a timed-out mutation is never retried.

`tests/verify_embedded_engine.py` runs a packaged host and actual plugin with synthetic OAuth-shaped data. A test-only fetch wrapper redirects provider traffic to localhost and rejects every other external destination. This tests the local integration, not live OAuth availability or a production subscription. `engine/test/boundaries.test.mjs` verifies DPAPI payloads above Windows Credential Manager's blob limit and concurrent account pins.
