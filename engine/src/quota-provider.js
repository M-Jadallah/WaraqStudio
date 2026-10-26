import { failure, quotaProjection, reference } from './rotation-policy.js';

// This release has no approved reader. Never load verification from settings,
// a provider payload, an RPC input, or the offline schema-inspection receipt.
export const ROTATION_SUPPORT = Object.freeze({ available: false, reason: 'official_source_unverified' });

export class QuotaProvider {
  #read; #verification; #clock; #random; #timeoutMs;
  #active = 0; #waiting = []; #pending = new Map(); #cache = new Map(); #generations = new Map();
  #sequence = 0; #closed = false; #controllers = new Set(); #accountTails = new Map();

  constructor({ read, verification = ROTATION_SUPPORT, clock = Date.now, random = Math.random, timeoutMs = 8000 } = {}) {
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 8000) throw failure('INVALID_QUOTA_TIMEOUT');
    this.#read = read;
    // verification is a trusted adapter dependency, not quota data. Production
    // uses the default unavailable adapter; tests inject synthetic readers.
    this.#verification = Object.freeze({ available: verification.available === true, reason: verification.reason });
    this.#clock = clock;
    this.#random = random;
    this.#timeoutMs = timeoutMs;
  }

  get supported() { return !this.#closed && this.#verification.available && typeof this.#read === 'function'; }
  get support() { return { available: this.supported, reason: this.supported ? '' : 'official_source_unverified' }; }
  #key(account, scope) { return JSON.stringify([reference(account.accountRef), reference(account.bindingVersion), reference(scope)]); }
  peek(account, scope) { return structuredClone(this.#cache.get(this.#key(account, scope)) ?? null); }

  invalidate(accountRef) {
    reference(accountRef);
    this.#generations.set(accountRef, (this.#generations.get(accountRef) ?? 0) + 1);
    for (const key of this.#cache.keys()) if (JSON.parse(key)[0] === accountRef) this.#cache.delete(key);
  }

  async #slot() {
    if (this.#closed) throw failure('PROVIDER_CLOSED');
    if (this.#active < 2) { this.#active++; return; }
    await new Promise((resolve, reject) => this.#waiting.push({ resolve, reject }));
  }

  #release() {
    const waiter = this.#waiting.shift();
    if (waiter) waiter.resolve(); // Transfer the occupied slot; never exceed two.
    else this.#active--;
  }

  refresh(account, scope) {
    if (!this.supported) return Promise.reject(failure(this.#closed ? 'PROVIDER_CLOSED' : 'ROTATION_UNSUPPORTED'));
    const key = this.#key(account, scope);
    if (this.#pending.has(key)) return this.#pending.get(key);
    const cached = this.#cache.get(key);
    if (cached?.nextRefreshAt > this.#clock()) return Promise.resolve(structuredClone(cached));
    const generation = this.#generations.get(account.accountRef) ?? 0;
    const preceding = this.#accountTails.get(account.accountRef) ?? Promise.resolve();
    const operation = preceding.then(() => this.#refresh(account, scope, key, generation)).finally(() => this.#pending.delete(key));
    const tail = operation.then(() => {}, () => {}).finally(() => {
      if (this.#accountTails.get(account.accountRef) === tail) this.#accountTails.delete(account.accountRef);
    });
    this.#accountTails.set(account.accountRef, tail);
    this.#pending.set(key, operation);
    return operation;
  }

  async #refresh(account, scope, key, generation) {
    await this.#slot();
    const controller = new AbortController();
    this.#controllers.add(controller);
    // A verified reader must honor AbortSignal. Retain its slot until it settles
    // so even a broken reader cannot cause extra concurrent upstream requests.
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      if (this.#closed) throw failure('PROVIDER_CLOSED');
      const raw = await this.#read({ accountRef: account.accountRef, bindingVersion: account.bindingVersion, scope, signal: controller.signal });
      if (this.#closed || generation !== (this.#generations.get(account.accountRef) ?? 0)) throw failure('STALE_BINDING');
      if (controller.signal.aborted) throw failure('USAGE_UNAVAILABLE');
      const snapshot = quotaProjection(raw);
      if (snapshot.accountRef !== account.accountRef || snapshot.bindingVersion !== account.bindingVersion || snapshot.scope !== scope) throw failure('STALE_BINDING');
      if (snapshot.status !== 'available') throw failure('USAGE_UNAVAILABLE');
      const result = { ...snapshot, sequence: ++this.#sequence, failures: 0, lastAttemptAt: this.#clock(), nextRefreshAt: 0 };
      this.#cache.set(key, result);
      return structuredClone(result);
    } catch (error) {
      if (this.#closed || generation !== (this.#generations.get(account.accountRef) ?? 0) || error?.safeCode === 'STALE_BINDING') {
        if (!this.#closed && generation === (this.#generations.get(account.accountRef) ?? 0)) this.#cache.delete(key);
        throw failure(this.#closed ? 'PROVIDER_CLOSED' : 'STALE_BINDING');
      }
      const previous = this.#cache.get(key);
      const failures = Math.min(10, (previous?.failures ?? 0) + 1);
      const delay = Math.min(300_000, 30_000 * 2 ** (failures - 1));
      const retryAfter = Number.isFinite(error?.retryAfterMs) && error.retryAfterMs > 0 ? error.retryAfterMs : 0;
      const result = {
        ...previous, accountRef: account.accountRef, bindingVersion: account.bindingVersion, scope,
        status: 'unavailable', failures, lastAttemptAt: this.#clock(),
        nextRefreshAt: this.#clock() + Math.max(retryAfter, Math.min(300_000, delay * (1 + Math.max(0, Math.min(1, this.#random())) * 0.1))),
      };
      this.#cache.set(key, result);
      return structuredClone(result);
    } finally {
      clearTimeout(timer);
      this.#controllers.delete(controller);
      this.#release();
    }
  }

  close() {
    this.#closed = true;
    for (const controller of this.#controllers) controller.abort();
    for (const waiter of this.#waiting.splice(0)) waiter.reject(failure('PROVIDER_CLOSED'));
    this.#cache.clear();
  }
}
