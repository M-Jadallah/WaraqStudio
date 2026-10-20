import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
export interface Lease { attemptId: string; jobId: string; workerId: string; model: string; directory: string; accountRef: string; state: string; failure?: {kind:string; confirmed:boolean; retryAtMs?:number}; events: {type:string; at:number; accountRef:string; status?:number; kind?:string; retryAtMs?:number}[] }
const key = Symbol.for("WaraqStudio.accounts.state.v1");
const host = globalThis as typeof globalThis & { [key: symbol]: any };
if (!host[key]) Object.defineProperty(host,key,{value:{leases:new Map<string,Lease>(),current:new AsyncLocalStorage<Lease>(),references:new Map<string,string>()}});
export const leases: Map<string,Lease> = host[key].leases;
const current: AsyncLocalStorage<Lease> = host[key].current;
export const references: Map<string,string> = host[key].references;
export function identityRef(account: any): string {
  const key = [account.accountId ?? "", account.accountUserId ?? "", account.organizationId ?? "", account.email ?? "", account.addedAt ?? ""].join("\0");
  const hash = "acct_" + createHash("sha256").update(key).digest("hex").slice(0,32);
  return hash;
}
export function accountRef(account: any): string { const ref = identityRef(account); return references.get(ref) ?? ref; }
export function pinnedExclusions(accounts: any[], excluded?: ReadonlySet<number>): ReadonlySet<number> | undefined {
  const lease = current.getStore();
  if (!lease) return excluded;
  const result = new Set(excluded);
  for (const account of accounts) if (accountRef(account) !== lease.accountRef) result.add(account.index);
  return result;
}
export async function withAttempt<T>(directory: string, operation: () => Promise<T>): Promise<T> {
  const lease = [...leases.values()].find(x => x.state === "active" && resolve(x.directory).toLowerCase() === resolve(directory).toLowerCase());
  if (!lease) throw new Error("ATTEMPT_REQUIRED");
  return current.run(lease, operation);
}
export function recordClassified(kind: string, retryAtMs?:number): void {
  const lease = current.getStore();
  if (!lease) return;
  lease.state = kind;
  lease.failure = {kind,confirmed:true,...(retryAtMs ? {retryAtMs} : {})};
  lease.events.push({type:"failure",at:Date.now(),accountRef:lease.accountRef,kind,...(retryAtMs ? {retryAtMs} : {})});
}
export function recordResponse(response: Response): Response {
  const lease = current.getStore();
  if (!lease) throw new Error("ATTEMPT_REQUIRED");
  lease.events.push({ type: "response", at: Date.now(), accountRef: lease.accountRef, status:response.status });
  if (response.ok) return response;
  if (!lease.failure) { lease.state="failed"; lease.failure={kind:response.status===429 ? "rate_limited" : "provider_error",confirmed:false}; }
  const code = lease.failure.kind === "quota" ? "usage_limit_reached" : lease.failure.kind === "reauth" ? "account_auth_failed" : lease.failure.kind === "rate_limited" ? "rate_limit_exceeded" : "provider_request_failed";
  // The desktop owns a fresh-attempt retry. Return a non-retryable transport
  // status to stop the host's generic 429/5xx loop; RPC retains the real cause.
  return new Response(JSON.stringify({error:{code,message:code}}),{status:400,headers:{"content-type":"application/json"}});
}
