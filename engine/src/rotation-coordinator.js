import { randomUUID } from 'node:crypto';
import { QuotaProvider } from './quota-provider.js';
import { circularOrder, DEFAULT_THRESHOLD, evaluateEligibility, exactOrder, failure, RECOVERY_CONFIRM_MS, reference, threshold, WAIT_RECHECK_MS } from './rotation-policy.js';

function initialState() {
  return { schemaVersion: 1, policy: { enabled: false, remainingThreshold: DEFAULT_THRESHOLD, revision: 0 },
    queueOrder: [], queueRevision: 0, activeAccountRef: null, activeBinding: null, cursor: null, scope: null,
    epoch: 0, state: 'disabled', reason: '', nextCheckAt: 0, accounts: [], recovery: {}, owners: [], recoveryHolds: [], receipts: [], finishedIds: [] };
}

function restoreState(saved) {
  const state = initialState();
  if (saved == null) return state;
  if (saved.schemaVersion !== 1 || !Array.isArray(saved.queueOrder) || new Set(saved.queueOrder).size !== saved.queueOrder.length ||
      !Array.isArray(saved.owners) || !Array.isArray(saved.recoveryHolds) || saved.owners.length + saved.recoveryHolds.length > 64 ||
      !Number.isSafeInteger(saved.epoch) || saved.epoch < 0 || !Number.isSafeInteger(saved.policy?.revision) || saved.policy.revision < 0 || typeof saved.policy.enabled !== 'boolean') throw failure('ROTATION_STATE_INVALID');
  for (const ref of saved.queueOrder) reference(ref);
  threshold(saved.policy.remainingThreshold);
  const holds = [...saved.recoveryHolds, ...saved.owners].map(owner => ({
    attemptId: reference(owner.attemptId), accountRef: reference(owner.accountRef), epoch: owner.epoch,
  }));
  if (new Set(holds.map(owner => owner.attemptId)).size !== holds.length || holds.some(owner => !Number.isSafeInteger(owner.epoch) || owner.epoch < 0)) throw failure('ROTATION_STATE_INVALID');
  // Never resurrect live leases, cached eligibility, or recovery confirmations.
  return { ...state, policy: { enabled: saved.policy.enabled, remainingThreshold: saved.policy.remainingThreshold, revision: saved.policy.revision }, queueOrder: [...saved.queueOrder],
    queueRevision: Number.isSafeInteger(saved.queueRevision) && saved.queueRevision >= 0 ? saved.queueRevision : 0,
    cursor: saved.queueOrder.includes(saved.cursor) ? saved.cursor : null, epoch: saved.epoch, recoveryHolds: holds,
    state: holds.length ? 'needs_action' : 'checking', reason: holds.length ? 'execution_unverified' : '',
    receipts: Array.isArray(saved.receipts) ? saved.receipts.slice(-64).map(receipt => ({ id: reference(receipt.id), signature: String(receipt.signature), revision: receipt.revision })) : [],
    finishedIds: Array.isArray(saved.finishedIds) ? saved.finishedIds.slice(-256).map(reference) : [] };
}

export class RotationCoordinator {
  #quota; #persist; #proveStopped; #clock; #maximum; #state;
  #tail = Promise.resolve(); #faulted = false;

  constructor({ quota = new QuotaProvider(), persist, proveStopped = async () => false, clock = Date.now, maxConcurrency = 5, saved = null } = {}) {
    if (typeof persist !== 'function') throw failure('ROTATION_PERSISTENCE_REQUIRED');
    if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 16) throw failure('INVALID_CONCURRENCY');
    this.#quota = quota; this.#persist = persist; this.#proveStopped = proveStopped; this.#clock = clock; this.#maximum = maxConcurrency;
    this.#state = restoreState(saved);
  }

  get supported() { return this.#quota.supported; }
  snapshot() { return structuredClone(this.#state); }
  status() {
    const s = this.#state;
    return { state: this.#faulted ? 'needs_action' : !this.supported ? 'unsupported' : s.state,
      reason: this.#faulted ? 'storage_unavailable' : !this.supported ? 'official_source_unverified' : s.reason,
      activeAccountRef: s.activeAccountRef, epoch: s.epoch, policyRevision: s.policy.revision, queueRevision: s.queueRevision,
      nextCheckAt: s.nextCheckAt, inFlight: s.owners.length, recoveryHolds: s.recoveryHolds.length };
  }

  #mutate(operation) {
    const run = this.#tail.then(async () => {
      if (this.#faulted) throw failure('ROTATION_STORAGE_UNAVAILABLE');
      const next = structuredClone(this.#state);
      const result = operation(next);
      if (JSON.stringify(next) !== JSON.stringify(this.#state)) {
        try { await this.#persist(structuredClone(next)); }
        catch { this.#faulted = true; throw failure('ROTATION_STORAGE_UNAVAILABLE'); }
        this.#state = next;
      }
      return result ?? this.status();
    });
    this.#tail = run.then(() => {}, () => {});
    return run;
  }

  setAccounts(accounts) {
    if (!Array.isArray(accounts)) return Promise.reject(failure('INVALID_ACCOUNTS'));
    const rows = accounts.map(row => ({ accountRef: reference(row.accountRef), bindingVersion: reference(row.bindingVersion),
      enabled: row.enabled === true, loginState: row.loginState === 'ready' ? 'ready' : 'needs_auth',
      providerEligible: row.providerEligible === true, blockedUntil: Number.isFinite(row.blockedUntil) ? row.blockedUntil : 0 }));
    if (new Set(rows.map(row => row.accountRef)).size !== rows.length) return Promise.reject(failure('INVALID_ACCOUNTS'));
    return this.#mutate(next => {
      const refs = rows.map(row => row.accountRef);
      if (next.owners.some(owner => !rows.some(row => row.accountRef === owner.accountRef && row.bindingVersion === owner.bindingVersion))) throw failure('ACCOUNT_BUSY');
      const order = [...next.queueOrder.filter(ref => refs.includes(ref)), ...refs.filter(ref => !next.queueOrder.includes(ref))];
      if (JSON.stringify(order) !== JSON.stringify(next.queueOrder)) { next.queueOrder = order; next.queueRevision++; }
      next.accounts = rows;
      this.#advance(next, next.scope ?? 'codex');
    });
  }

  configure({ enabled, remainingThreshold, expectedRevision, operationId }) {
    if (typeof enabled !== 'boolean') return Promise.reject(failure('INVALID_POLICY'));
    threshold(remainingThreshold); reference(operationId);
    if (enabled && !this.supported) return Promise.reject(failure('ROTATION_UNSUPPORTED'));
    const signature = JSON.stringify([enabled, remainingThreshold, expectedRevision]);
    return this.#mutate(next => {
      const receipt = next.receipts.find(item => item.id === operationId);
      if (receipt) { if (receipt.signature !== signature) throw failure('OPERATION_CONFLICT'); return { revision: receipt.revision }; }
      if (expectedRevision !== next.policy.revision) throw failure('POLICY_REVISION_CONFLICT');
      next.policy = { enabled, remainingThreshold, revision: next.policy.revision + 1 };
      next.recovery = {};
      next.receipts = [...next.receipts, { id: operationId, signature, revision: next.policy.revision }].slice(-64);
      this.#advance(next, next.scope ?? 'codex');
      return { revision: next.policy.revision };
    });
  }

  reorder(order, expectedRevision) {
    return this.#mutate(next => {
      if (expectedRevision !== next.queueRevision) throw failure('QUEUE_REVISION_CONFLICT');
      next.queueOrder = exactOrder(order, next.accounts); next.queueRevision++;
    });
  }

  #eligibility(next, account, scope) {
    const snapshot = this.#quota.peek(account, scope);
    const result = evaluateEligibility(account, snapshot, next.policy.remainingThreshold, this.#clock(), scope);
    const key = JSON.stringify([account.accountRef, account.bindingVersion, scope]);
    if (result.reason === 'threshold_reached') next.recovery[key] = { firstAboveAt: null, sequence: null };
    else if (result.eligible && next.recovery[key]) {
      const recovering = next.recovery[key];
      if (recovering.firstAboveAt == null) { recovering.firstAboveAt = snapshot.observedAt; recovering.sequence = snapshot.sequence; }
      if (snapshot.sequence > recovering.sequence && snapshot.observedAt - recovering.firstAboveAt >= RECOVERY_CONFIRM_MS) delete next.recovery[key];
      else return { ...result, eligible: false, reason: 'confirming_recovery' };
    } else if (!result.eligible && next.recovery[key]) next.recovery[key] = { firstAboveAt: null, sequence: null };
    return result;
  }

  #advance(next, scope) {
    const now = this.#clock();
    next.nextCheckAt = 0;
    if (!next.policy.enabled) { next.state = next.owners.length ? 'draining' : 'disabled'; next.reason = ''; return; }
    if (!this.supported) { next.state = 'unsupported'; next.reason = 'official_source_unverified'; return; }
    if (next.recoveryHolds.length || next.owners.some(owner => owner.uncertain)) { next.state = 'needs_action'; next.reason = 'execution_unverified'; return; }
    if (next.owners.length && next.scope !== scope) throw failure('SCOPE_BUSY');
    const decisions = new Map(next.accounts.map(account => [account.accountRef, this.#eligibility(next, account, scope)]));
    const current = next.accounts.find(account => account.accountRef === next.activeAccountRef && account.bindingVersion === next.activeBinding);
    if (current && decisions.get(current.accountRef).eligible) { next.state = 'running'; next.reason = ''; next.scope = scope; return; }
    if (next.owners.length) { next.state = 'draining'; next.reason = decisions.get(next.activeAccountRef)?.reason ?? 'account_unavailable'; return; }
    const selected = circularOrder(next.queueOrder, next.cursor).find(ref => decisions.get(ref)?.eligible);
    if (selected) {
      const account = next.accounts.find(row => row.accountRef === selected);
      next.activeAccountRef = selected; next.activeBinding = account.bindingVersion; next.cursor = selected;
      next.scope = scope; next.epoch++; next.state = 'running'; next.reason = ''; return;
    }
    next.activeAccountRef = null; next.activeBinding = null; next.scope = scope;
    next.state = 'waiting'; next.reason = [...decisions.values()].some(item => item.reason === 'waiting_usage' || item.reason === 'unsupported_scope') ? 'waiting_usage' : 'no_eligible_account';
    next.nextCheckAt = now + WAIT_RECHECK_MS;
    for (const account of next.accounts) {
      const snapshot = this.#quota.peek(account, scope);
      const recovery = next.recovery[JSON.stringify([account.accountRef, account.bindingVersion, scope])];
      const dates = [account.blockedUntil, snapshot?.nextRefreshAt, recovery?.firstAboveAt == null ? 0 : recovery.firstAboveAt + RECOVERY_CONFIRM_MS,
        ...(snapshot?.windows ?? []).map(window => window.resetsAt)];
      for (const date of dates) if (Number.isFinite(date) && date > now) next.nextCheckAt = Math.min(next.nextCheckAt, date);
    }
  }

  evaluate(scope = 'codex') { reference(scope); return this.#mutate(next => this.#advance(next, scope)); }

  beginAttempt(attemptId, scope = 'codex') {
    reference(attemptId); reference(scope);
    return this.#mutate(next => {
      if (next.finishedIds.includes(attemptId)) throw failure('ATTEMPT_EXISTS');
      const previous = next.owners.find(owner => owner.attemptId === attemptId);
      if (previous) { if (previous.scope !== scope || previous.closing) throw failure('ATTEMPT_EXISTS'); return structuredClone(previous); }
      this.#advance(next, scope);
      if (next.state !== 'running') return { denied: next.state === 'unsupported' ? 'ROTATION_UNSUPPORTED' : 'NO_ELIGIBLE_ACCOUNT' };
      if (next.owners.length >= this.#maximum) throw failure('CONCURRENCY_LIMIT');
      const owner = { attemptId, accountRef: next.activeAccountRef, bindingVersion: next.activeBinding, epoch: next.epoch, scope,
        requests: [], tools: [], replaySafe: true, uncertain: false, closing: false };
      next.owners.push(owner);
      return structuredClone(owner);
    }).then(result => { if (result.denied) throw failure(result.denied); return result; });
  }

  admit(attemptId, kind = 'request', replaySafe = false) {
    if (!['request', 'tool'].includes(kind)) return Promise.reject(failure('INVALID_ADMISSION'));
    return this.#mutate(next => {
      const owner = next.owners.find(row => row.attemptId === attemptId);
      if (!owner || owner.closing) throw failure('ATTEMPT_NOT_ACTIVE');
      this.#advance(next, owner.scope);
      if (next.state !== 'running' || owner.epoch !== next.epoch || owner.accountRef !== next.activeAccountRef) return { denied: 'REQUEST_PAUSED' };
      const collection = kind === 'request' ? owner.requests : owner.tools;
      if (collection.length >= 64) throw failure('CONCURRENCY_LIMIT');
      const token = { id: randomUUID(), attemptId, epoch: owner.epoch, kind };
      collection.push(token.id);
      if (kind === 'tool' && replaySafe !== true) owner.replaySafe = false;
      return token;
    }).then(result => { if (result.denied) throw failure(result.denied); return result; });
  }

  complete(token, { uncertain = false } = {}) {
    return this.#mutate(next => {
      const owner = next.owners.find(row => row.attemptId === token.attemptId && row.epoch === token.epoch);
      const collection = token.kind === 'request' ? owner?.requests : token.kind === 'tool' ? owner?.tools : null;
      if (!collection?.includes(token.id)) throw failure('ADMISSION_NOT_FOUND');
      collection.splice(collection.indexOf(token.id), 1);
      if (uncertain) { owner.uncertain = true; owner.replaySafe = false; }
      this.#advance(next, owner.scope);
    });
  }

  async closeAttempt(attemptId) {
    const owner = await this.#mutate(next => {
      const row = next.owners.find(item => item.attemptId === attemptId);
      if (!row) throw failure('ATTEMPT_NOT_FOUND');
      if (row.closing) throw failure('ATTEMPT_BUSY');
      if (row.requests.length || row.tools.length) throw failure('ATTEMPT_BUSY');
      row.closing = true;
      return structuredClone(row);
    });
    let stopped = false;
    try { stopped = await this.#proveStopped(owner) === true; } catch { /* Unknown proof remains held. */ }
    return this.#mutate(next => {
      const row = next.owners.find(item => item.attemptId === attemptId);
      if (!stopped) { row.closing = false; row.uncertain = true; row.replaySafe = false; this.#advance(next, row.scope); return { closed: false, canReplay: false }; }
      next.owners = next.owners.filter(item => item.attemptId !== attemptId);
      next.finishedIds = [...next.finishedIds, attemptId].slice(-256);
      this.#advance(next, row.scope);
      return { closed: true, canReplay: row.replaySafe && !row.uncertain };
    });
  }

  async reconcile() {
    const holds = this.snapshot().recoveryHolds;
    const proven = [];
    for (const hold of holds) { try { if (await this.#proveStopped(hold) === true) proven.push(hold.attemptId); } catch { /* Retain unknown owner. */ } }
    return this.#mutate(next => {
      next.recoveryHolds = next.recoveryHolds.filter(hold => !proven.includes(hold.attemptId));
      next.finishedIds = [...new Set([...next.finishedIds, ...proven])].slice(-256);
      this.#advance(next, next.scope ?? 'codex');
    });
  }
}
