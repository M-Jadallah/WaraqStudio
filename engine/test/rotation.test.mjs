import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateEligibility, quotaProjection, threshold } from '../src/rotation-policy.js';
import { QuotaProvider } from '../src/quota-provider.js';
import { RotationCoordinator } from '../src/rotation-coordinator.js';
import { gatedRequest } from '../src/request-gate.js';
import { ReplaySafety } from '../src/replay-safety.js';

const account = id => ({ accountRef: id, bindingVersion: 'binding_1', enabled: true, loginState: 'ready', providerEligible: true, blockedUntil: 0 });
const snapshot = (row, now, remaining = 80, scope = 'codex') => ({
  accountRef: row.accountRef, bindingVersion: row.bindingVersion, scope,
  observedAt: now, coverageComplete: true, ordinaryUsageAllowed: true, status: 'available',
  requiredWindowIds: ['five_hour', 'weekly'],
  windows: [{ id: 'five_hour', remainingPercent: remaining, windowMinutes: 300, resetsAt: now + 18_000_000 },
    { id: 'weekly', remainingPercent: remaining, windowMinutes: 10080, resetsAt: now + 604_800_000 }],
});
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

async function fixture({ count = 5, maximum = 5, proveStopped = async () => true, persist } = {}) {
  let now = 2_000_000;
  const rows = Array.from({ length: count }, (_, i) => account('account_' + (i + 1)));
  const values = new Map(rows.map(row => [row.accountRef, 80]));
  const clock = () => now;
  const quota = new QuotaProvider({ verification: { available: true }, clock, random: () => 0,
    read: async ({ accountRef, bindingVersion, scope }) => snapshot({ accountRef, bindingVersion }, now, values.get(accountRef), scope) });
  const writes = [];
  const coordinator = new RotationCoordinator({ quota, clock, maxConcurrency: maximum, proveStopped,
    persist: persist ?? (async next => { writes.push(structuredClone(next)); }) });
  await Promise.all(rows.map(row => quota.refresh(row, 'codex')));
  await coordinator.setAccounts(rows);
  await coordinator.configure({ enabled: true, remainingThreshold: 20, expectedRevision: 0, operationId: 'initial' });
  return { rows, quota, coordinator, values, clock, writes,
    advance: ms => { now += ms; },
    update: async (id, remaining) => { values.set(id, remaining); await quota.refresh(rows.find(row => row.accountRef === id), 'codex'); } };
}

for (const value of [0, 0.5, 20, 20.123456, 100]) test(`valid threshold ${value}`, () => assert.equal(threshold(value), value));
for (const value of [true, false, '20', null, NaN, Infinity, -0.1, 100.1]) test(`invalid threshold ${String(value)}`, () => assert.throws(() => threshold(value), /INVALID_THRESHOLD/));

test('eligibility uses the smallest applicable window and exact strict boundary', () => {
  const row = account('one'), raw = snapshot(row, 1000, 60);
  raw.windows[1].remainingPercent = 20;
  assert.equal(evaluateEligibility(row, raw, 20, 1000, 'codex').reason, 'threshold_reached');
  raw.windows[1].remainingPercent = 20.01;
  assert.equal(evaluateEligibility(row, raw, 20, 1000, 'codex').eligible, true);
  assert.equal(evaluateEligibility(row, raw, 100, 1000, 'codex').eligible, false);
});

test('stale, future, expired, missing and wrong-binding readings never enable an account', () => {
  const row = account('one'), raw = snapshot(row, 1000);
  for (const changed of [null, { ...raw, observedAt: 1001 }, { ...raw, bindingVersion: 'other' },
    { ...raw, windows: raw.windows.slice(1) }, { ...raw, coverageComplete: false },
    { ...raw, ordinaryUsageAllowed: false }, { ...raw, windows: [{ ...raw.windows[0], resetsAt: 1000 }, raw.windows[1]] }]) {
    assert.equal(evaluateEligibility(row, changed, 20, 1000, 'codex').eligible, false);
  }
  assert.equal(evaluateEligibility(row, raw, 20, 61_000, 'codex').reason, 'waiting_usage');
  assert.equal(evaluateEligibility(row, raw, 20, 1000, 'other_scope').reason, 'unsupported_scope');
});

test('unknown and malformed numeric fields cannot become zero/full or carry nested secrets', () => {
  const row = account('one'), raw = snapshot(row, 1000);
  const projected = quotaProjection({ ...raw, accessToken: 'SECRET', windows: [{ ...raw.windows[0], remainingPercent: { secret: 'SECRET' } }, raw.windows[1]] });
  assert.equal(JSON.stringify(projected).includes('SECRET'), false);
  assert.equal(evaluateEligibility(row, projected, 20, 1000, 'codex').eligible, false);
});

test('default reader cannot activate even if a payload claims verification', async () => {
  let called = false;
  const quota = new QuotaProvider({ read: async () => { called = true; return { verified: true }; } });
  const coordinator = new RotationCoordinator({ quota, persist: async () => {} });
  await assert.rejects(quota.refresh(account('one'), 'codex'), /ROTATION_UNSUPPORTED/);
  await assert.rejects(coordinator.configure({ enabled: true, remainingThreshold: 20, expectedRevision: 0, operationId: 'enable' }), /ROTATION_UNSUPPORTED/);
  assert.equal(called, false);
  assert.equal(coordinator.status().state, 'unsupported');
});

test('sequential failover skips low accounts, drains all owners and preserves one active identity', async () => {
  const f = await fixture();
  await f.update('account_2', 15);
  await f.update('account_4', 20);
  const owners = await Promise.all(['a', 'b', 'c'].map(id => f.coordinator.beginAttempt(id)));
  assert.deepEqual(owners.map(owner => owner.accountRef), ['account_1', 'account_1', 'account_1']);
  const permits = await Promise.all(owners.map(owner => f.coordinator.admit(owner.attemptId)));
  await f.update('account_1', 20);
  await assert.rejects(f.coordinator.admit('a'), /REQUEST_PAUSED/);
  assert.equal(f.coordinator.status().state, 'draining');
  await assert.rejects(f.coordinator.beginAttempt('d'), /NO_ELIGIBLE_ACCOUNT/);
  await assert.rejects(f.coordinator.closeAttempt('a'), /ATTEMPT_BUSY/);
  for (let i = 0; i < permits.length; i++) {
    await f.coordinator.complete(permits[i]);
    await f.coordinator.closeAttempt(owners[i].attemptId);
    if (i < permits.length - 1) assert.equal(f.coordinator.status().activeAccountRef, 'account_1');
  }
  assert.equal(f.coordinator.status().activeAccountRef, 'account_3');
  await f.update('account_3', 19);
  await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().activeAccountRef, 'account_5');
  f.quota.close();
});

test('exhausted queue waits, reset time is only a probe, and recovery requires two readings', async () => {
  const f = await fixture({ count: 2 });
  await f.update('account_1', 20); await f.update('account_2', 20);
  await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().state, 'waiting');
  const epoch = f.coordinator.status().epoch;
  await Promise.all(Array.from({ length: 20 }, () => f.coordinator.evaluate()));
  assert.equal(f.coordinator.status().epoch, epoch);
  await f.update('account_1', 21);
  await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().state, 'waiting');
  f.advance(30_000);
  await f.coordinator.evaluate(); // Time alone cannot confirm a new reading.
  assert.equal(f.coordinator.status().state, 'waiting');
  await f.update('account_1', 21);
  await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().activeAccountRef, 'account_1');
});

test('recovery above the threshold cannot preempt a healthy active account', async () => {
  const f = await fixture({ count: 2 });
  await f.update('account_1', 10); await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().activeAccountRef, 'account_2');
  await f.update('account_1', 90); await f.coordinator.evaluate(); f.advance(30_000);
  await f.update('account_1', 90); await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().activeAccountRef, 'account_2');
  await f.update('account_2', 20); await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().activeAccountRef, 'account_1');
});

test('concurrent claims respect the configured bound and begin is idempotent while owned', async () => {
  const f = await fixture({ maximum: 2 });
  const outcomes = await Promise.allSettled(['one', 'two', 'three', 'four'].map(id => f.coordinator.beginAttempt(id)));
  assert.equal(outcomes.filter(result => result.status === 'fulfilled').length, 2);
  assert.equal(f.coordinator.snapshot().owners.length, 2);
  await f.coordinator.beginAttempt('one');
  assert.equal(f.coordinator.snapshot().owners.length, 2);
  await f.coordinator.closeAttempt('one');
  await assert.rejects(f.coordinator.beginAttempt('one'), /ATTEMPT_EXISTS/);
});

test('policy updates are revisioned, idempotent and stop the next request immediately', async () => {
  const f = await fixture();
  await f.coordinator.beginAttempt('one');
  const policy = { enabled: true, remainingThreshold: 90, expectedRevision: 1, operationId: 'raise' };
  assert.deepEqual(await f.coordinator.configure(policy), { revision: 2 });
  assert.deepEqual(await f.coordinator.configure(policy), { revision: 2 });
  await assert.rejects(f.coordinator.configure({ ...policy, remainingThreshold: 10 }), /OPERATION_CONFLICT/);
  await assert.rejects(f.coordinator.admit('one'), /REQUEST_PAUSED/);
  await assert.rejects(f.coordinator.configure({ ...policy, operationId: 'stale' }), /POLICY_REVISION_CONFLICT/);
});

test('queue reorder is atomic, versioned, exact and does not preempt the current account', async () => {
  const f = await fixture({ count: 3 });
  const revision = f.coordinator.status().queueRevision;
  await assert.rejects(f.coordinator.reorder(['account_2', 'account_2', 'account_1'], revision), /INVALID_QUEUE_ORDER/);
  await f.coordinator.reorder(['account_3', 'account_1', 'account_2'], revision);
  await assert.rejects(f.coordinator.reorder(['account_1', 'account_2', 'account_3'], revision), /QUEUE_REVISION_CONFLICT/);
  assert.equal(f.coordinator.status().activeAccountRef, 'account_1');
  await f.update('account_1', 20); await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().activeAccountRef, 'account_2');
});

test('cannot delete or replace an owned identity; disabling drains it', async () => {
  const f = await fixture({ count: 2 });
  await f.coordinator.beginAttempt('one');
  await assert.rejects(f.coordinator.setAccounts(f.rows.slice(1)), /ACCOUNT_BUSY/);
  await assert.rejects(f.coordinator.setAccounts([{ ...f.rows[0], bindingVersion: 'replacement' }, f.rows[1]]), /ACCOUNT_BUSY/);
  await f.coordinator.setAccounts([{ ...f.rows[0], enabled: false }, f.rows[1]]);
  assert.equal(f.coordinator.status().state, 'draining');
});

test('no unverified stop releases an owner; a later proof permits release but never unsafe replay', async () => {
  let stopped = false;
  const f = await fixture({ proveStopped: async () => stopped });
  await f.coordinator.beginAttempt('one');
  assert.deepEqual(await f.coordinator.closeAttempt('one'), { closed: false, canReplay: false });
  assert.equal(f.coordinator.status().state, 'needs_action');
  await assert.rejects(f.coordinator.beginAttempt('two'), /NO_ELIGIBLE_ACCOUNT/);
  stopped = true;
  assert.deepEqual(await f.coordinator.closeAttempt('one'), { closed: true, canReplay: false });
});

test('restart restores cursor and proof holds but never live leases or cached eligibility', async () => {
  const f = await fixture({ count: 2 });
  await f.coordinator.beginAttempt('one');
  const saved = f.coordinator.snapshot();
  let proof = false;
  const quota = new QuotaProvider({ verification: { available: true }, clock: f.clock, read: async () => { throw Error('offline'); } });
  const restored = new RotationCoordinator({ quota, saved, clock: f.clock, persist: async () => {}, proveStopped: async () => proof });
  await restored.setAccounts(f.rows);
  assert.equal(restored.snapshot().owners.length, 0);
  assert.equal(restored.status().recoveryHolds, 1);
  await restored.reconcile();
  assert.equal(restored.status().state, 'needs_action');
  proof = true; await restored.reconcile();
  assert.equal(restored.status().state, 'waiting');
  assert.equal(restored.snapshot().cursor, 'account_1');
  await assert.rejects(restored.beginAttempt('one'), /ATTEMPT_EXISTS/);
});

test('journal failure prevents admission and latches without forwarding storage text', async () => {
  let fail = false;
  const f = await fixture({ persist: async () => { if (fail) throw Error('SECRET_STORAGE_DETAIL'); } });
  await f.coordinator.beginAttempt('one');
  fail = true;
  await assert.rejects(f.coordinator.admit('one'), /^Error: ROTATION_STORAGE_UNAVAILABLE$/);
  assert.equal(f.coordinator.status().reason, 'storage_unavailable');
  await assert.rejects(f.coordinator.beginAttempt('two'), /ROTATION_STORAGE_UNAVAILABLE/);
});

test('quota reads coalesce and serialize each account with at most two readers total', async () => {
  let active = 0, maximum = 0;
  const perAccount = new Map(), gate = deferred();
  const quota = new QuotaProvider({ verification: { available: true }, read: async ({ accountRef, bindingVersion, scope }) => {
    active++; maximum = Math.max(maximum, active);
    perAccount.set(accountRef, (perAccount.get(accountRef) ?? 0) + 1);
    assert.equal(perAccount.get(accountRef), 1);
    await gate.promise;
    perAccount.set(accountRef, perAccount.get(accountRef) - 1); active--;
    return snapshot({ accountRef, bindingVersion }, 1000, 70, scope);
  } });
  const first = quota.refresh(account('one'), 'codex');
  assert.equal(first, quota.refresh(account('one'), 'codex'));
  const pending = [first, quota.refresh(account('one'), 'other'), quota.refresh(account('two'), 'codex'), quota.refresh(account('three'), 'codex')];
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(active, 2); gate.resolve(); await Promise.all(pending);
  assert.equal(maximum, 2);
  quota.close();
});

test('failed quota reads retain old values as unavailable and respect provider Retry-After', async () => {
  let fail = false, calls = 0, now = 1000;
  const row = account('one');
  const quota = new QuotaProvider({ verification: { available: true }, clock: () => now, random: () => 0,
    read: async () => { calls++; if (fail) throw Object.assign(Error('SECRET'), { retryAfterMs: 600_000 }); return snapshot(row, now); } });
  await quota.refresh(row, 'codex'); fail = true;
  const failed = await quota.refresh(row, 'codex');
  assert.equal(failed.status, 'unavailable'); assert.equal(failed.observedAt, 1000);
  assert.equal(failed.nextRefreshAt, 601_000);
  now += 30_000; await quota.refresh(row, 'codex');
  assert.equal(calls, 2); assert.equal(JSON.stringify(failed).includes('SECRET'), false);
});

test('late quota result is discarded after account invalidation', async () => {
  const gate = deferred(), row = account('one');
  const quota = new QuotaProvider({ verification: { available: true }, read: async () => { await gate.promise; return snapshot(row, 1000); } });
  const pending = quota.refresh(row, 'codex');
  quota.invalidate(row.accountRef); gate.resolve();
  await assert.rejects(pending, /STALE_BINDING/);
  assert.equal(quota.peek(row, 'codex'), null);
});

test('quota timeout aborts a cooperative reader and never makes its response fresh', async () => {
  const row = account('one'); let aborted = false;
  const quota = new QuotaProvider({ verification: { available: true }, timeoutMs: 5, clock: () => 1000,
    read: async ({ signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => { aborted = true; reject(Error('aborted')); }, { once: true })) });
  const result = await quota.refresh(row, 'codex');
  assert.equal(aborted, true); assert.equal(result.status, 'unavailable');
  quota.close();
});

test('HTTP headers cannot release an SSE request before terminal body completion', async () => {
  const f = await fixture({ count: 2 });
  await f.coordinator.beginAttempt('one');
  let stream;
  const response = await gatedRequest(f.coordinator, 'one', async () => new Response(new ReadableStream({ start(controller) { stream = controller; } }), { headers: { 'content-type': 'text/event-stream' } }));
  assert.equal(f.coordinator.snapshot().owners[0].requests.length, 1);
  await f.update('account_1', 20); await f.coordinator.evaluate();
  await assert.rejects(f.coordinator.admit('one'), /REQUEST_PAUSED/);
  stream.enqueue(new TextEncoder().encode('data: {"type":"response.com'));
  stream.enqueue(new TextEncoder().encode('pleted"}\n\n')); stream.close();
  await response.text();
  assert.equal(f.coordinator.snapshot().owners[0].requests.length, 0);
  assert.equal(f.coordinator.status().activeAccountRef, 'account_1');
  await f.coordinator.closeAttempt('one');
  assert.equal(f.coordinator.status().activeAccountRef, 'account_2');
});

test('truncated SSE and rejected transport retain uncertainty', async () => {
  const f = await fixture(); await f.coordinator.beginAttempt('one');
  const response = await gatedRequest(f.coordinator, 'one', async () => new Response('data: {"type":"response.created"}\n\n', { headers: { 'content-type': 'text/event-stream' } }));
  await response.text();
  assert.equal(f.coordinator.status().state, 'needs_action');
  assert.deepEqual(await f.coordinator.closeAttempt('one'), { closed: true, canReplay: false });
  await f.coordinator.beginAttempt('two');
  await assert.rejects(gatedRequest(f.coordinator, 'two', async () => { throw Error('SECRET_REQUEST_HEADERS'); }), /^Error: PROVIDER_TRANSPORT_UNCERTAIN$/);
  assert.equal(f.coordinator.status().state, 'needs_action');
});

test('unknown or unfinished tool effects cannot be replayed', () => {
  const safe = new ReplaySafety(); safe.begin('read', 'read_only');
  assert.equal(safe.canReplay, false); safe.complete('read'); assert.equal(safe.canReplay, true);
  safe.begin('write', 'isolated_idempotent'); safe.complete('write'); assert.equal(safe.canReplay, true);
  const unknown = new ReplaySafety(); unknown.begin('shell'); unknown.complete('shell');
  assert.equal(unknown.canReplay, false); assert.equal(unknown.summary().unknownEffects, true);
  assert.equal(JSON.stringify(unknown.summary()).includes('shell'), false);
});

test('a stop proof waiting on I/O never holds the policy decision queue', async () => {
  const proof = deferred(), entered = deferred();
  const f = await fixture({ proveStopped: async () => { entered.resolve(); return proof.promise; } });
  await f.coordinator.beginAttempt('one');
  const closing = f.coordinator.closeAttempt('one'); await entered.promise;
  await f.coordinator.configure({ enabled: true, remainingThreshold: 90, expectedRevision: 1, operationId: 'during_proof' });
  assert.equal(f.coordinator.status().state, 'draining');
  proof.resolve(true); await closing;
  assert.equal(f.coordinator.status().state, 'waiting');
});

test('manual disable drains existing work and never resumes itself', async () => {
  const f = await fixture(); await f.coordinator.beginAttempt('one');
  const token = await f.coordinator.admit('one');
  await f.coordinator.configure({ enabled: false, remainingThreshold: 20, expectedRevision: 1, operationId: 'disable' });
  assert.equal(f.coordinator.status().state, 'draining');
  await assert.rejects(f.coordinator.admit('one'), /REQUEST_PAUSED/);
  await f.coordinator.complete(token); await f.coordinator.closeAttempt('one');
  await f.update('account_1', 100); await f.coordinator.evaluate();
  assert.equal(f.coordinator.status().state, 'disabled');
});

test('unknown tool effects drain safely but do not permit replay', async () => {
  const f = await fixture({ count: 2 }); await f.coordinator.beginAttempt('one');
  const tool = await f.coordinator.admit('one', 'tool');
  await f.update('account_1', 20); await f.coordinator.evaluate();
  await assert.rejects(f.coordinator.admit('one', 'tool'), /REQUEST_PAUSED/);
  await assert.rejects(f.coordinator.closeAttempt('one'), /ATTEMPT_BUSY/);
  await f.coordinator.complete(tool);
  assert.deepEqual(await f.coordinator.closeAttempt('one'), { closed: true, canReplay: false });
  assert.equal(f.coordinator.status().activeAccountRef, 'account_2');
});

test('simultaneous recovery follows the cursor and cannot start two switches', async () => {
  const f = await fixture({ count: 3 });
  for (const row of f.rows) await f.update(row.accountRef, 20);
  await f.coordinator.evaluate();
  for (const row of f.rows) await f.update(row.accountRef, 30);
  await f.coordinator.evaluate(); f.advance(30_000);
  for (const row of f.rows) await f.update(row.accountRef, 30);
  const before = f.coordinator.status().epoch;
  await Promise.all(Array.from({ length: 10 }, () => f.coordinator.evaluate()));
  assert.equal(f.coordinator.status().activeAccountRef, 'account_2');
  assert.equal(f.coordinator.status().epoch, before + 1);
});

test('wrong-identity response invalidates the previous cache immediately', async () => {
  const row = account('one'); let wrong = false;
  const quota = new QuotaProvider({ verification: { available: true }, read: async () => snapshot(wrong ? account('other') : row, 1000) });
  await quota.refresh(row, 'codex'); wrong = true;
  await assert.rejects(quota.refresh(row, 'codex'), /STALE_BINDING/);
  assert.equal(quota.peek(row, 'codex'), null);
});

test('failure of one account does not hide or refresh another account reading', async () => {
  let fail = false;
  const quota = new QuotaProvider({ verification: { available: true }, clock: () => 1000, read: async ({ accountRef }) => {
    if (fail && accountRef === 'two') throw Error('offline');
    return snapshot(account(accountRef), 1000);
  } });
  await Promise.all(['one', 'two'].map(id => quota.refresh(account(id), 'codex'))); fail = true;
  await quota.refresh(account('two'), 'codex');
  assert.equal(quota.peek(account('one'), 'codex').status, 'available');
  assert.equal(quota.peek(account('two'), 'codex').status, 'unavailable');
});

test('closing a reader aborts active reads and rejects queued reads', async () => {
  let entered = 0;
  const quota = new QuotaProvider({ verification: { available: true }, read: async ({ signal }) => {
    entered++;
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Error('closed')), { once: true }));
  } });
  const pending = ['one', 'two', 'three', 'four'].map(id => quota.refresh(account(id), 'codex'));
  const outcomes = Promise.allSettled(pending);
  await new Promise(resolve => setImmediate(resolve));
  quota.close();
  assert.equal(entered, 2);
  assert.ok((await outcomes).every(result => result.status === 'rejected' && result.reason.safeCode === 'PROVIDER_CLOSED'));
});

test('cancelled SSE remains uncertain until native stop proof', async () => {
  const f = await fixture(); await f.coordinator.beginAttempt('one');
  const response = await gatedRequest(f.coordinator, 'one', async () => new Response(new ReadableStream({ pull() {} }), { headers: { 'content-type': 'text/event-stream' } }));
  await response.body.cancel();
  assert.equal(f.coordinator.status().state, 'needs_action');
  assert.deepEqual(await f.coordinator.closeAttempt('one'), { closed: true, canReplay: false });
});

test('malformed persisted state never silently drops active ownership', () => {
  const args = { persist: async () => {} };
  assert.throws(() => new RotationCoordinator({ ...args, saved: { schemaVersion: 99 } }), /ROTATION_STATE_INVALID/);
  assert.throws(() => new RotationCoordinator(), /ROTATION_PERSISTENCE_REQUIRED/);
});

test('projection cannot silently discard an invalid or oversized required limit', () => {
  const row = account('one');
  for (const requiredWindowIds of [['five_hour', null], Array.from({ length: 33 }, (_, i) => 'limit_' + i)]) {
    const projected = quotaProjection({ ...snapshot(row, 1000), requiredWindowIds });
    assert.equal(projected.coverageComplete, false);
    assert.equal(evaluateEligibility(row, projected, 20, 1000, 'codex').eligible, false);
  }
});
