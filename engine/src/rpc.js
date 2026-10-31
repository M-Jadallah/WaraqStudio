import { Rpc } from "@opencode/plugin/rpc";
import { timingSafeEqual, randomUUID } from "node:crypto";
import { join, resolve } from "node:path";
import { z } from "zod";
import { AccountManager } from "../vendor/lib/accounts.js";
import { loadAccounts, withAccountStorageTransaction } from "../vendor/lib/storage.js";
import { promises as fs, storageRoot, seal, unseal } from "../vendor/lib/storage/encrypted-fs.js";
import { writeFileAtomic } from "../vendor/lib/storage/atomic-write.js";
import { leases, accountRef, identityRef, references } from "../vendor/lib/embedded-state.js";
import { getModelFamily } from "../vendor/lib/prompts/codex.js";
import { startLoopbackFlow } from "../vendor/lib/auth/loopback-flow.js";
import { createDeviceCodeSession, completeDeviceCodeSession } from "../vendor/lib/auth/device-code.js";
import { resolveAndPersistAccountSelection, persistAccountPool } from "../vendor/lib/auth/login-runner.js";
import { ensureCodexUsageAccessToken, fetchCodexUsage, parseCodexUsagePayload } from "../vendor/lib/codex-usage.js";
import { exportAccountsFile, importAccountsFile, transferError } from "./account-transfer.js";

const metadataPath = join(storageRoot, "waraq-metadata.json");
let metadata;
const stateKey=Symbol.for("WaraqStudio.accounts.rpc.v1");
if (!globalThis[stateKey]) Object.defineProperty(globalThis,stateKey,{value:{queue:Promise.resolve(),metadataQueue:Promise.resolve(),usageRequests:new Map(),usageActive:0,usageWaiters:[],authAttempts:new Map()}});
const shared=globalThis[stateKey];
const {usageRequests,usageWaiters,authAttempts}=shared;
function error(code) { return Object.assign(new Error(code), { safeCode: code }); }
async function getMetadata() {
  if (metadata) return metadata;
  if (shared.metadata) return metadata=shared.metadata;
  if (shared.loading) return metadata=await shared.loading;
  shared.loading=(async()=>{
  await fs.mkdir(storageRoot, { recursive: true, mode: 0o700 });
  // Probe DPAPI before exposing even a healthy empty account list.
  unseal(seal("probe"));
  try { metadata = JSON.parse(await fs.readFile(metadataPath, "utf8")); }
  catch (e) { if (e.code !== "ENOENT") throw error("STORAGE_UNAVAILABLE"); metadata = { rows: {}, bindings: {}, preferred: null }; }
  for (const [key, ref] of Object.entries(metadata.bindings)) references.set(key, ref);
  shared.metadata=metadata;
  return metadata;
  })();
  try { return metadata=await shared.loading; } finally { shared.loading=null; }
}
async function persistMetadata() {
  const operation = shared.metadataQueue.then(() => writeFileAtomic(metadataPath, JSON.stringify(metadata)));
  shared.metadataQueue = operation.catch(() => {});
  await operation;
}
function authorizing(ref) { return [...authAttempts.values()].some(a => a.ref === ref && a.state === "pending"); }
function quotaReset(ref) {
  const quota = metadata.rows[ref]?.quota;
  return Math.max(0, ...[quota?.primary, quota?.secondary].filter(w => w && w.usedPercent >= 100 && w.resetAtMs > Date.now()).map(w => w.resetAtMs));
}
function busy(ref) { return [...leases.values()].filter(x => x.accountRef === ref && x.state !== "closed").length; }
function publicAccount(account, ref) {
  const row = metadata.rows[ref] || {};
  const cooldownUntil = Math.max(row.quotaBlockedUntil || 0, quotaReset(ref), account?.quotaExhaustedUntil || 0, account?.coolingDownUntil || 0, ...Object.values(account?.rateLimitResetTimes || {}).filter(Number.isFinite));
  const enabled = row.enabled !== false && account?.enabled !== false;
  return { accountRef: ref, alias: row.alias || account?.accountLabel || "Account", email: account?.email || null,
    plan: account?.planType || null, enabled, priority: row.priority || 0, canExport: !!account?.refreshToken,
    loginState: row.requiresLogin ? "reauth" : account?.refreshToken ? "ready" : "staged", health: !enabled ? "disabled" : cooldownUntil > Date.now() ? "cooldown" : account ? "ready" : "staged",
    bindingVersion: row.bindingVersion || "1", preferred: metadata.preferred === ref, busy: busy(ref) > 0, inFlight: busy(ref), cooldownUntil: cooldownUntil || null, quota: row.quota || null };
}
async function list() {
  await getMetadata();
  const pool = await loadAccounts();
  const found = new Set();
  let added = false;
  const accounts = (pool?.accounts || []).map(a => { const ref = accountRef(a); found.add(ref);
    if (!metadata.rows[ref]) { metadata.rows[ref] = {alias:a.accountLabel || "Account",bindingVersion:randomUUID(),enabled:a.enabled !== false,priority:0}; added = true; }
    return publicAccount(a, ref); });
  if (added) await persistMetadata();
  for (const ref of Object.keys(metadata.rows)) if (!found.has(ref)) accounts.push(publicAccount(null, ref));
  return { accounts };
}
async function lookup(ref) {
  const pool = await loadAccounts();
  const account = pool?.accounts.find(a => accountRef(a) === ref);
  if (!account && !metadata.rows[ref]) throw error("ACCOUNT_NOT_FOUND");
  return { pool, account };
}
function authView(attempt) { return { attemptId: attempt.id, accountRef: attempt.ref, state: attempt.state, ...(attempt.error ? { error: {code: attempt.error} } : {}) }; }
async function completeAuth(attempt, tokenOperation) {
  try {
    const result = await tokenOperation();
    if (attempt.state === "cancelled") return;
    if (result.type !== "success") { attempt.state = "failed"; attempt.error = "AUTH_FAILED"; return; }
    attempt.completing = true;
    const selection = await resolveAndPersistAccountSelection(result, { persistSelections: persistAccountPool });
    // Bind the user-created opaque reference to the canonical stored identity.
    const pool = await loadAccounts();
    const saved = pool?.accounts.find(a => a.refreshToken === selection.primary.refresh && (!selection.primary.accountId || a.accountId === selection.primary.accountId));
    if (!saved) throw error("AUTH_PERSIST_FAILED");
    const canonical = identityRef(saved);
    const existing = metadata.bindings[canonical];
    if (existing && existing !== attempt.ref && metadata.rows[existing]) throw error("DUPLICATE_ACCOUNT");
    metadata.bindings[canonical] = attempt.ref;
    references.set(canonical, attempt.ref);
    metadata.rows[attempt.ref].bindingVersion = randomUUID();
    metadata.rows[attempt.ref].quota = null;
    metadata.rows[attempt.ref].requiresLogin = false;
    await persistMetadata();
    attempt.state = "success";
  } catch (e) { if (attempt.state !== "cancelled") { attempt.state = "failed"; attempt.error = e.safeCode === "DUPLICATE_ACCOUNT" ? "DUPLICATE_ACCOUNT" : "AUTH_FAILED"; } }
  finally { attempt.completing = false; attempt.close?.(); }
}
const fields = {
  health: {}, "accounts.list": {}, "account.create": { alias: z.string().trim().min(1).max(120) },
  "accounts.export": { accountRefs: z.array(z.string().min(1).max(160)).min(1).max(20), filePath: z.string().max(32768), password: z.string().max(512) },
  "accounts.import": { filePath: z.string().max(32768), password: z.string().max(512) },
  "auth.begin": {accountRef:z.string(), method:z.enum(["browser","device"])},
  "auth.status": {attemptId:z.string(),code:z.string().max(4096).optional()}, "auth.cancel": {attemptId:z.string()},
  "account.update": {accountRef:z.string(),alias:z.string().trim().min(1).max(120).optional(),enabled:z.boolean().optional(),priority:z.number().int().min(-100).max(100).optional()},
  "account.prefer": {accountRef:z.string()}, "account.remove": {accountRef:z.string()},
  "usage.refresh": {accountRef:z.string().optional(),manual:z.boolean().optional()},
  "attempt.begin": {attemptId:z.string().min(1).max(128),jobId:z.string(),workerId:z.string(),model:z.string(),directory:z.string(),maxConcurrency:z.number().int().min(1).max(64)},
  "attempt.close": {attemptId:z.string(),outcome:z.string().optional()}, "attempt.resolve": {attemptId:z.string()},
};
const definition = Rpc.define({id:"waraq-accounts",methods:Object.fromEntries(Object.entries(fields).map(([name, shape]) => [name,{input:z.object({apiVersion:z.literal(1),capability:z.string(),...shape}).strict(),output:z.unknown()}])),events:{}});
async function dispatch(name, input, context) {
  await getMetadata();
  if (name === "health") return {apiVersion:1,pluginVersion:"6.29.0",storage:"dpapi",authMethods:[{id:"browser",label:"Browser"},{id:"device",label:"Device code"}]};
  if (name === "accounts.list") return list();
  if (name === "accounts.export" || name === "accounts.import") {
    if (shared.transferActive || usageRequests.size || [...leases.values()].some(a => a.state !== "closed") || [...authAttempts.values()].some(a => a.state === "pending" || a.completing)) throw transferError("TRANSFER_BUSY");
    shared.transferActive = true;
    try {
      return name === "accounts.export" ? await exportAccountsFile(input, { accountRef, rows: metadata.rows }) : await importAccountsFile(input);
    } catch (e) { throw e.safeCode ? e : transferError("TRANSFER_IO_FAILED"); }
    finally { shared.transferActive = false; }
  }
  if (name === "account.create") { const ref = "acct_"+randomUUID().replaceAll("-",""); metadata.rows[ref]={alias:input.alias,enabled:true,priority:0}; await persistMetadata(); return publicAccount(null,ref); }
  if (name.startsWith("account.")) {
    await lookup(input.accountRef);
    if (busy(input.accountRef) || authorizing(input.accountRef)) throw error("ACCOUNT_BUSY");
    if (name === "account.remove") {
      await withAccountStorageTransaction(async (pool,persist) => { if (!pool) return; pool.accounts=pool.accounts.filter(a=>accountRef(a)!==input.accountRef); pool.activeIndex=0; pool.activeIndexByFamily={}; await persist(pool); });
      delete metadata.rows[input.accountRef];
      for (const [canonical, ref] of Object.entries(metadata.bindings)) if (ref === input.accountRef) { delete metadata.bindings[canonical]; references.delete(canonical); }
      if(metadata.preferred===input.accountRef) metadata.preferred=null;
      await persistMetadata(); return {accountRef:input.accountRef,removed:true};
    }
    if (name === "account.prefer") metadata.preferred=input.accountRef;
    else {
      const row = metadata.rows[input.accountRef] ||= {};
      for(const key of ["alias","enabled","priority"]) if(input[key]!==undefined) row[key]=input[key];
      if(input.enabled!==undefined) await withAccountStorageTransaction(async (pool,persist)=>{if(!pool)return;const account=pool.accounts.find(a=>accountRef(a)===input.accountRef);if(account)account.enabled=input.enabled;await persist(pool);});
    }
    await persistMetadata(); return (await list()).accounts.find(a=>a.accountRef===input.accountRef);
  }
  if (name === "auth.begin") {
    await lookup(input.accountRef);
    if (busy(input.accountRef)) throw error("ACCOUNT_BUSY");
    if ([...authAttempts.values()].some(a=>a.state==="pending")) throw error("AUTH_BUSY");
    const attempt={id:randomUUID(),ref:input.accountRef,state:"pending"};
    authAttempts.set(attempt.id,attempt);
    if(input.method==="browser") {
      const flow=await startLoopbackFlow({openBrowser:false,forceNewLogin:true});
      if(flow.type!=="ready") {attempt.state="failed";throw error("AUTH_BEGIN_FAILED");}
      attempt.close=flow.close; void completeAuth(attempt,()=>flow.waitAndExchange());
      return {...authView(attempt),url:flow.url};
    }
    const started=await createDeviceCodeSession();
    if(started.type!=="ready") {attempt.state="failed";throw error("AUTH_BEGIN_FAILED");}
    void completeAuth(attempt,()=>completeDeviceCodeSession(started.session));
    return {...authView(attempt),url:started.session.verificationUrl,userCode:started.session.userCode};
  }
  if(name.startsWith("auth.")) {
    const attempt=authAttempts.get(input.attemptId);if(!attempt)throw error("AUTH_ATTEMPT_NOT_FOUND");
    if(name==="auth.cancel"){attempt.state="cancelled";attempt.close?.();}
    return authView(attempt);
  }
  if(name==="usage.refresh") {
    if (shared.transferActive) throw transferError("TRANSFER_BUSY");
    if(!input.accountRef) {
      const result=[];
      await Promise.all((await list()).accounts.filter(x=>x.loginState==="ready").map(async row => {
        try { result.push(await dispatch(name,{...input,accountRef:row.accountRef},context)); }
        catch { result.push(row); }
      }));
      return {accounts:result};
    }
    if (usageRequests.has(input.accountRef)) return usageRequests.get(input.accountRef);
    const operation = (async () => {
    if (shared.usageActive >= 2) await new Promise(resolve => usageWaiters.push(resolve));
    shared.usageActive++;
    try {
    const {pool,account}=await lookup(input.accountRef);if(!account)throw error("ACCOUNT_NOT_AUTHENTICATED");
    const version = metadata.rows[input.accountRef]?.bindingVersion;
    try {
      const {accessToken}=await ensureCodexUsageAccessToken({storage:pool,account});
      const summary=parseCodexUsagePayload(await fetchCodexUsage({accountId:account.accountId,organizationId:account.organizationId,accessToken,timeoutMs:8000}));
      const window=x=>({usedPercent:x.usedPercent??null,resetAtMs:x.resetAtMs??null,windowMinutes:x.windowMinutes??null});
      if (!metadata.rows[input.accountRef] || metadata.rows[input.accountRef].bindingVersion !== version) throw error("STALE_BINDING");
      const row=metadata.rows[input.accountRef] ||= {};
      row.quota={observedAt:Date.now(),source:"remote",primary:window(summary.primary),secondary:window(summary.secondary)};
      row.quotaBlockedUntil=quotaReset(input.accountRef);
      await persistMetadata();return publicAccount(account,input.accountRef);
    } catch { throw error("USAGE_UNAVAILABLE"); }
    } finally { shared.usageActive--; usageWaiters.shift()?.(); }
    })();
    usageRequests.set(input.accountRef, operation);
    try { return await operation; } finally { usageRequests.delete(input.accountRef); }
  }
  if(name==="attempt.begin") {
    if(leases.has(input.attemptId))throw error("ATTEMPT_EXISTS");
    if(resolve(input.directory).toLowerCase()!==resolve(context.location.directory).toLowerCase())throw error("LOCATION_MISMATCH");
    const active=[...leases.values()].filter(x=>x.state!=="closed");
    if(active.length>=input.maxConcurrency)throw error("CONCURRENCY_LIMIT");
    if(active.some(x=>resolve(x.directory).toLowerCase()===resolve(input.directory).toLowerCase()))throw error("LOCATION_BUSY");
    const manager=await AccountManager.loadFromDisk();
    try {
      const family=getModelFamily(input.model.replace(/^openai\//,""));
      const eligible=manager.getSelectionExplainability(family,input.model.replace(/^openai\//,""));
      const candidates=manager.getAccountsSnapshot().filter(a=>eligible.find(e=>e.index===a.index)?.eligible && metadata.rows[accountRef(a)]?.enabled!==false && !metadata.rows[accountRef(a)]?.requiresLogin && (metadata.rows[accountRef(a)]?.quotaBlockedUntil || 0) <= Date.now() && quotaReset(accountRef(a)) <= Date.now());
      candidates.sort((a,b)=>Number(accountRef(b)===metadata.preferred)-Number(accountRef(a)===metadata.preferred)||(metadata.rows[accountRef(b)]?.priority||0)-(metadata.rows[accountRef(a)]?.priority||0)||busy(accountRef(a))-busy(accountRef(b)));
      const account=candidates[0];if(!account)throw error("NO_ELIGIBLE_ACCOUNT");
      const ref=accountRef(account); const lease={...input,accountRef:ref,state:"active",events:[{type:"selected",at:Date.now(),accountRef:ref}]};delete lease.capability;delete lease.apiVersion;
      leases.set(input.attemptId,lease);return {...lease,alias:publicAccount(account,ref).alias,email:account.email||null};
    } finally { manager.disposeShutdownHandler(true); }
  }
  const lease=leases.get(input.attemptId);if(!lease)throw error("ATTEMPT_NOT_FOUND");
  if(name==="attempt.close"){
    if (lease.failure?.kind === "quota") {
      (metadata.rows[lease.accountRef] ||= {}).quotaBlockedUntil=Math.max(Date.now()+30000,lease.failure.retryAtMs || 0); await persistMetadata();
    }
    if (lease.failure?.kind === "reauth" || lease.failure?.kind === "workspace_blocked") {
      (metadata.rows[lease.accountRef] ||= {}).requiresLogin = true; await persistMetadata();
    }
    lease.state="closed";lease.events.push({type:"closed",at:Date.now(),accountRef:lease.accountRef});
    const closed = [...leases.values()].filter(x => x.state === "closed");
    for (const old of closed.slice(0, Math.max(0, closed.length - 256))) leases.delete(old.attemptId);
  }
  return {...lease};
}
export async function createAccountsRpc(context, capability) {
  const expected=Buffer.from(capability);
  const handlers=Object.fromEntries(Object.keys(fields).map(name=>[name,async input=>{
    const supplied=Buffer.from(input.capability);
    if(supplied.length!==expected.length || !timingSafeEqual(supplied,expected))return {ok:false,error:{code:"UNAUTHORIZED"}};
    const execute=async()=>{try{return {ok:true,data:await dispatch(name,input,context)};}catch(e){return {ok:false,error:{code:e.safeCode||"ENGINE_UNAVAILABLE"}};}};
    if (["health","accounts.list","auth.status","attempt.resolve","usage.refresh"].includes(name)) return execute();
    const operation=shared.queue.then(execute);
    shared.queue=operation.then(()=>{},()=>{});return operation;
  }]));
  return context.rpc.register(definition,handlers);
}
