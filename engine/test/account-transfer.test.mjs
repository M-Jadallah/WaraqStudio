import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, readdir, rm, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const temporary = await mkdtemp(join(tmpdir(), 'waraq-transfer-'));
const root = join(temporary, 'private');
await mkdir(root);
process.env.WARAQ_ACCOUNTS_STORAGE_DIR = root;
const transfer = await import('../dist/src/account-transfer.js');
const store = await import('../dist/vendor/lib/storage/load-save.js');
const state = await import('../dist/vendor/lib/storage/state.js');
const encrypted = await import('../dist/vendor/lib/storage/encrypted-fs.js');
const bindings = await import('../dist/vendor/lib/embedded-state.js');
const password = '  عبارة أمان الملف  🔒  ';
const entry = (id, fields = {}) => ({ alias: 'الحساب ' + id, enabled: true, accountId: id,
  refreshToken: 'SYNTHETIC_REFRESH_' + id, accessToken: 'SYNTHETIC_ACCESS_' + id,
  expiresAt: Date.now() + 3600000, ...fields });
const stored = (id, fields = {}) => { const { alias, ...rest } = entry(id); return { ...rest, accountLabel: alias, addedAt: 1, lastUsed: 0, ...fields }; };
const pool = (accounts) => ({ version: 3, accounts, activeIndex: 0, activeIndexByFamily: {} });
const backup = join(temporary, 'حسابات.waraq-accounts');
const code = (expected) => e => e.safeCode === expected;
let encoded;

test('portable encryption preserves Arabic/spaces, hides every credential and randomizes each file', async () => {
  const data = { version: 1, accounts: [entry('one')] };
  encoded = await transfer.encryptAccounts(data, password);
  const again = await transfer.encryptAccounts(data, password);
  assert.notEqual(encoded, again);
  for (const secret of [password, 'SYNTHETIC_', 'الحساب', 'one']) assert.equal(encoded.includes(secret), false);
  assert.deepEqual(await transfer.decryptAccounts(encoded, password), data);
  await assert.rejects(() => transfer.decryptAccounts(encoded, password.trim()), code('TRANSFER_DECRYPT_FAILED'));
});
test('wrong password, changed salt/nonce/tag/ciphertext, truncated and unsupported files fail closed', async () => {
  await assert.rejects(() => transfer.decryptAccounts(encoded, 'wrong-password-123'), code('TRANSFER_DECRYPT_FAILED'));
  for (const field of ['salt', 'nonce', 'tag', 'ciphertext']) {
    const changed = JSON.parse(encoded), bytes = Buffer.from(changed[field], 'base64');
    bytes[0] ^= 1; changed[field] = bytes.toString('base64');
    await assert.rejects(() => transfer.decryptAccounts(JSON.stringify(changed), password), code('TRANSFER_DECRYPT_FAILED'));
  }
  await assert.rejects(() => transfer.decryptAccounts(encoded.slice(0, -5), password), code('TRANSFER_FORMAT_INVALID'));
  await assert.rejects(() => transfer.decryptAccounts(JSON.stringify({ ...JSON.parse(encoded), version: 2 }), password), code('TRANSFER_VERSION_UNSUPPORTED'));
  await assert.rejects(() => transfer.decryptAccounts(' '.repeat(transfer.MAX_TRANSFER_BYTES + 1), password), code('TRANSFER_FILE_TOO_LARGE'));
  for (const weak of ['', 'short', 'a'.repeat(257), '\ud800' + 'a'.repeat(20)])
    await assert.rejects(() => transfer.encryptAccounts({ version: 1, accounts: [entry('one')] }, weak), code('TRANSFER_PASSWORD_INVALID'));
});
test('selected export imports into independent DPAPI store, preserves names/disabled flag and skips existing credentials', async () => {
  state.setStoragePathDirect(join(root, 'source.json'));
  const first = stored('one'), second = stored('two');
  await store.saveAccounts(pool([first, second]));
  const ref = bindings.accountRef(first);
  assert.deepEqual(await transfer.exportAccountsFile({ accountRefs: [ref], filePath: backup, password },
    { accountRef: bindings.accountRef, rows: { [ref]: { alias: 'اسم خاص', enabled: false } } }), { exported: 1 });
  const bytes = await readFile(backup, 'utf8');
  assert.equal(bytes.includes('SYNTHETIC'), false);
  state.setStoragePathDirect(join(root, 'target.json'));
  assert.deepEqual(await transfer.importAccountsFile({ filePath: backup, password }), { imported: 1, skipped: 0, total: 1 });
  const loaded = await store.loadAccounts();
  assert.equal(loaded.accounts[0].accountLabel, 'اسم خاص');
  assert.equal(loaded.accounts[0].enabled, false);
  assert.equal(loaded.accounts[0].refreshToken, first.refreshToken);
  assert.ok((await readFile(join(root, 'target.json'))).subarray(0, 14).toString().startsWith('WARAQ-DPAPI-1'));
  // Rotated credentials, changed label and timestamps still denote one identity.
  loaded.accounts[0] = { ...loaded.accounts[0], refreshToken: 'LOCAL_NEW_TOKEN', accessToken: 'LOCAL_NEW_ACCESS', accountLabel: 'الاسم المحلي', lastUsed: 100 };
  await store.saveAccounts(loaded);
  const before = await readFile(join(root, 'target.json'));
  assert.deepEqual(await transfer.importAccountsFile({ filePath: backup, password }), { imported: 0, skipped: 1, total: 1 });
  assert.deepEqual(await readFile(join(root, 'target.json')), before);
  assert.equal((await store.loadAccounts()).accounts[0].refreshToken, 'LOCAL_NEW_TOKEN');
  for (const bad of ['wrong-password-123']) {
    await assert.rejects(() => transfer.importAccountsFile({ filePath: backup, password: bad }), code('TRANSFER_DECRYPT_FAILED'));
    assert.deepEqual(await readFile(join(root, 'target.json')), before);
  }
});
test('different workspace seats and workspace variants remain separate, duplicate file records are skipped', async () => {
  state.setStoragePathDirect(join(root, 'seats.json'));
  const a = entry('workspace', { organizationId: 'org-a', accountUserId: 'seat-a', accountIdSource: 'org' });
  const b = entry('workspace', { organizationId: 'org-a', accountUserId: 'seat-b', accountIdSource: 'org' });
  const c = entry('workspace', { organizationId: 'org-b', accountUserId: 'seat-a', accountIdSource: 'org' });
  await writeFile(backup, await transfer.encryptAccounts({ version: 1, accounts: [a, b, c, a] }, password));
  assert.deepEqual(await transfer.importAccountsFile({ filePath: backup, password }), { imported: 3, skipped: 1, total: 3 });
  assert.equal((await store.loadAccounts()).accounts.length, 3);
});
test('account limit and failed writes retain existing store/export and clean encrypted temporary files', async () => {
  const currentPath = join(root, 'limit.json');
  state.setStoragePathDirect(currentPath);
  await store.saveAccounts(pool(Array.from({ length: 20 }, (_, i) => stored('local-' + i))));
  await writeFile(backup, await transfer.encryptAccounts({ version: 1, accounts: [entry('new')] }, password));
  const before = await readFile(currentPath);
  await assert.rejects(() => transfer.importAccountsFile({ filePath: backup, password }), code('TRANSFER_LIMIT_EXCEEDED'));
  assert.deepEqual(await readFile(currentPath), before);
  await store.saveAccounts(pool([stored('existing')]));
  const originalStore = await readFile(currentPath), originalExport = await readFile(backup);
  const originalOpen = encrypted.promises.open;
  encrypted.promises.open = async function(path, ...args) {
    const handle = await originalOpen(path, ...args);
    if (!String(path).endsWith('.tmp')) return handle;
    return new Proxy(handle, { get(target, key) {
      if (key === 'writeFile') return async (...values) => { await target.writeFile(...values); throw Object.assign(new Error('synthetic disk full'), { code: 'ENOSPC' }); };
      const value = Reflect.get(target, key, target); return typeof value === 'function' ? value.bind(target) : value;
    } });
  };
  try {
    await assert.rejects(() => transfer.importAccountsFile({ filePath: backup, password }));
    const account = (await store.loadAccounts()).accounts[0];
    await assert.rejects(() => transfer.exportAccountsFile({ filePath: backup, password, accountRefs: [bindings.accountRef(account)] }, { accountRef: bindings.accountRef, rows: {} }));
  } finally { encrypted.promises.open = originalOpen; }
  assert.deepEqual(await readFile(currentPath), originalStore);
  assert.deepEqual(await readFile(backup), originalExport);
  assert.equal((await readdir(root)).some(name => name.endsWith('.tmp')), false);
  assert.equal((await readdir(temporary)).some(name => name.endsWith('.tmp')), false);
});
test('private storage paths and junction aliases are refused', async () => {
  await assert.rejects(() => transfer.transferPath(join(root, 'backup.waraq-accounts')), code('TRANSFER_PATH_INVALID'));
  await assert.rejects(() => transfer.transferPath(join(temporary, 'settings.json')), code('TRANSFER_PATH_INVALID'));
  const link = join(temporary, 'private-alias');
  await symlink(root, link, 'junction');
  await assert.rejects(() => transfer.transferPath(join(link, 'backup.waraq-accounts')), code('TRANSFER_PATH_INVALID'));
});
test('authenticated RPC enforces leases/auth/quota exclusion and only returns summaries', async () => {
  state.setStoragePathDirect(join(root, 'rpc.json'));
  await store.saveAccounts(pool([stored('rpc')]));
  const { createAccountsRpc } = await import('../dist/src/rpc.js');
  const handlers = await createAccountsRpc({ rpc: { register: (_definition, handlers) => handlers }, location: { directory: temporary } }, 'fixture-capability');
  const auth = { apiVersion: 1, capability: 'fixture-capability' };
  const rows = (await handlers['accounts.list'](auth)).data.accounts;
  assert.equal(rows[0].canExport, true);
  const input = { ...auth, filePath: backup, password, accountRefs: [rows[0].accountRef] };
  assert.equal((await handlers['accounts.export']({ ...input, capability: 'wrong' })).error.code, 'UNAUTHORIZED');
  const shared = globalThis[Symbol.for('WaraqStudio.accounts.rpc.v1')];
  for (const kind of ['lease', 'auth', 'usage']) {
    if (kind === 'lease') bindings.leases.set('fixture', { state: 'active' });
    if (kind === 'auth') shared.authAttempts.set('fixture', { state: 'cancelled', completing: true });
    if (kind === 'usage') shared.usageRequests.set('fixture', Promise.resolve());
    assert.equal((await handlers['accounts.export'](input)).error.code, 'TRANSFER_BUSY');
    bindings.leases.delete('fixture'); shared.authAttempts.delete('fixture'); shared.usageRequests.delete('fixture');
  }
  const exporting = handlers['accounts.export'](input);
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal((await handlers['usage.refresh']({ ...auth, accountRef: rows[0].accountRef })).error.code, 'TRANSFER_BUSY');
  assert.deepEqual(await exporting, { ok: true, data: { exported: 1 } });
  assert.equal(shared.transferActive, false);
  assert.deepEqual(await handlers['accounts.import']({ ...auth, filePath: backup, password }), { ok: true, data: { imported: 0, skipped: 1, total: 1 } });
});
test.after(async () => { await rm(temporary, { recursive: true, force: true }); });
