import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.WARAQ_ACCOUNTS_STORAGE_DIR = await mkdtemp(join(tmpdir(), 'waraq-dpapi-'));
const storage = await import('../dist/vendor/lib/storage/encrypted-fs.js');
const binding = await import('../dist/vendor/lib/embedded-state.js');
test('DPAPI encrypts large pool, temporary writes and snapshots; refuses plaintext', async () => {
  const value = JSON.stringify({refreshToken:'SYNTHETIC_SECRET_'+'x'.repeat(9000)});
  const path = join(storage.storageRoot,'pool.json');
  await storage.promises.writeFile(path,value);
  const bytes = await readFile(path);
  assert.equal(bytes.includes(Buffer.from('SYNTHETIC_SECRET_')),false);
  assert.equal(await storage.promises.readFile(path,'utf8'),value);
  const temporary=join(storage.storageRoot,'pool.tmp');
  const handle=await storage.promises.open(temporary,'w');
  await handle.writeFile(value); await handle.close();
  assert.equal(await storage.promises.readFile(temporary,'utf8'),value);
  assert.throws(()=>storage.unseal(Buffer.from(value)),/PLAINTEXT_STORAGE_REFUSED/);
  const corrupt=Buffer.from(bytes); corrupt[corrupt.length-10]^=0xff;
  assert.throws(()=>storage.unseal(corrupt),/DPAPI_DECRYPT_FAILED/);
});
test('concurrent locations retain their own account even in nested retries', async () => {
  const rows=[{accountId:'one',index:0},{accountId:'two',index:1}];
  const first={attemptId:'one',directory:join(storage.storageRoot,'one'),accountRef:binding.accountRef(rows[0]),state:'active',events:[]};
  const second={attemptId:'two',directory:join(storage.storageRoot,'two'),accountRef:binding.accountRef(rows[1]),state:'active',events:[]};
  binding.leases.set(first.attemptId,first); binding.leases.set(second.attemptId,second);
  const selected=await Promise.all([first,second].map(lease=>binding.withAttempt(lease.directory,async()=>{
    await new Promise(resolve=>setTimeout(resolve,10));
    return [...binding.pinnedExclusions(rows)];
  })));
  assert.deepEqual(selected,[[1],[0]]);
  await binding.withAttempt(first.directory,async()=>binding.recordClassified('quota',Date.now()+3600000));
  assert.equal(first.failure.kind,'quota');
  await assert.rejects(()=>binding.withAttempt(first.directory,async()=>{}),/ATTEMPT_REQUIRED/);
  assert.equal(second.state,'active');
  await assert.rejects(()=>binding.withAttempt(join(storage.storageRoot,'unknown'),async()=>{}),/ATTEMPT_REQUIRED/);
});
test.after(async()=>{await rm(storage.storageRoot,{recursive:true,force:true});});
