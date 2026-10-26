// Pure policy. Times are Unix milliseconds; no credentials or I/O belong here.
export const DEFAULT_THRESHOLD = 20;
export const MAX_QUOTA_AGE_MS = 60_000;
export const RECOVERY_CONFIRM_MS = 30_000;
export const WAIT_RECHECK_MS = 300_000;

export function failure(code) {
  return Object.assign(new Error(code), { safeCode: code });
}

export function reference(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,160}$/.test(value)) throw failure('INVALID_REFERENCE');
  return value;
}

export function threshold(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > 100) throw failure('INVALID_THRESHOLD');
  return value;
}

export function exactOrder(order, accounts) {
  const refs = accounts.map(account => reference(account.accountRef));
  if (!Array.isArray(order) || order.length !== refs.length || new Set(order).size !== order.length || order.some(ref => !refs.includes(ref))) {
    throw failure('INVALID_QUEUE_ORDER');
  }
  return [...order];
}

export function circularOrder(order, cursor) {
  const index = order.indexOf(cursor);
  return index < 0 ? [...order] : [...order.slice(index + 1), ...order.slice(0, index + 1)];
}

export function evaluateEligibility(account, snapshot, remainingThreshold, now, scope) {
  threshold(remainingThreshold);
  const deny = reason => ({ eligible: false, reason, remainingPercent: null });
  if (account.enabled !== true) return deny('disabled');
  if (account.loginState !== 'ready') return deny('needs_auth');
  if (account.providerEligible !== true) return deny('provider_blocked');
  if (Number.isFinite(account.blockedUntil) && account.blockedUntil > now) return deny('cooldown');
  if (!snapshot || snapshot.accountRef !== account.accountRef || snapshot.bindingVersion !== account.bindingVersion) return deny('waiting_usage');
  if (snapshot.scope !== scope || snapshot.coverageComplete !== true) return deny('unsupported_scope');
  if (snapshot.status !== 'available' || !Number.isFinite(snapshot.observedAt) || snapshot.observedAt <= 0 || snapshot.observedAt > now || now - snapshot.observedAt >= MAX_QUOTA_AGE_MS) return deny('waiting_usage');
  if (snapshot.ordinaryUsageAllowed !== true) return deny(snapshot.ordinaryUsageAllowed === false ? 'provider_blocked' : 'waiting_usage');
  const required = snapshot.requiredWindowIds;
  if (!Array.isArray(required) || !required.length || required.some(id => typeof id !== 'string' || !id) || new Set(required).size !== required.length || !Array.isArray(snapshot.windows)) return deny('waiting_usage');
  const values = [];
  for (const id of required) {
    const matches = snapshot.windows.filter(window => window?.id === id);
    if (matches.length !== 1) return deny('waiting_usage');
    const window = matches[0];
    if (typeof window.remainingPercent !== 'number' || !Number.isFinite(window.remainingPercent) || window.remainingPercent < 0 || window.remainingPercent > 100 || !Number.isFinite(window.windowMinutes) || window.windowMinutes <= 0) return deny('waiting_usage');
    if (window.resetsAt != null && (!Number.isFinite(window.resetsAt) || window.resetsAt <= now)) return deny('waiting_usage');
    values.push(window.remainingPercent);
  }
  const remainingPercent = Math.min(...values);
  return { eligible: remainingPercent > remainingThreshold, reason: remainingPercent > remainingThreshold ? '' : 'threshold_reached', remainingPercent };
}

// Explicit allowlist: a reader's response may never carry credentials onward.
export function quotaProjection(raw) {
  if (!raw || typeof raw !== 'object') throw failure('USAGE_UNAVAILABLE');
  const number = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
  const requiredValid = Array.isArray(raw.requiredWindowIds) && raw.requiredWindowIds.length > 0 && raw.requiredWindowIds.length <= 32
    && raw.requiredWindowIds.every(id => typeof id === 'string' && /^[A-Za-z0-9_.:-]{1,160}$/.test(id));
  const windowsValid = Array.isArray(raw.windows) && raw.windows.length <= 64;
  return {
    accountRef: raw.accountRef, bindingVersion: raw.bindingVersion, scope: raw.scope,
    observedAt: number(raw.observedAt), coverageComplete: raw.coverageComplete === true && requiredValid && windowsValid,
    ordinaryUsageAllowed: typeof raw.ordinaryUsageAllowed === 'boolean' ? raw.ordinaryUsageAllowed : null,
    status: raw.status === 'available' ? 'available' : 'unavailable',
    requiredWindowIds: Array.isArray(raw.requiredWindowIds) ? raw.requiredWindowIds.filter(id => typeof id === 'string').slice(0, 32) : [],
    windows: Array.isArray(raw.windows) ? raw.windows.slice(0, 64).map(window => ({
      id: typeof window?.id === 'string' ? window.id : null,
      remainingPercent: number(window?.remainingPercent), windowMinutes: number(window?.windowMinutes), resetsAt: number(window?.resetsAt),
    })) : [],
  };
}
