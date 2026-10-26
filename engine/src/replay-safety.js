import { reference, failure } from './rotation-policy.js';

// Classification is supplied by a trusted tool adapter, never inferred from a
// shell command/name or accepted from model prose. Unknown tools are not safe.
export class ReplaySafety {
  #calls = new Map(); #uncertain = false;
  begin(callId, classification = 'unknown') {
    reference(callId);
    if (this.#calls.has(callId)) throw failure('TOOL_CALL_EXISTS');
    if (!['read_only', 'isolated_idempotent', 'unknown'].includes(classification)) throw failure('INVALID_TOOL_CLASSIFICATION');
    this.#calls.set(callId, { classification, completed: false });
  }
  complete(callId, confirmed = true) {
    const call = this.#calls.get(callId);
    if (!call || call.completed) throw failure('TOOL_CALL_NOT_ACTIVE');
    call.completed = true;
    if (confirmed !== true) this.#uncertain = true;
  }
  markUncertain() { this.#uncertain = true; }
  get canReplay() {
    return !this.#uncertain && [...this.#calls.values()].every(call => call.completed && call.classification !== 'unknown');
  }
  summary() {
    return { canReplay: this.canReplay, activeTools: [...this.#calls.values()].filter(call => !call.completed).length,
      unknownEffects: this.#uncertain || [...this.#calls.values()].some(call => call.classification === 'unknown') };
  }
}
