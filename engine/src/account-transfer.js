// Portable credential backups. Plaintext exists only in memory; the account
// transaction still owns DPAPI persistence on the receiving Windows profile.
import { createCipheriv, createDecipheriv, randomBytes, scrypt } from "node:crypto";
import { promisify } from "node:util";
import { open, realpath, lstat } from "node:fs/promises";
import { dirname, basename, join, resolve, relative, isAbsolute, extname } from "node:path";
import { z } from "zod";
import { withAccountStorageTransaction } from "../vendor/lib/storage/load-save.js";
import { deduplicateAccountsForStorage } from "../vendor/lib/storage/identity.js";
import { writeFileAtomic } from "../vendor/lib/storage/atomic-write.js";
import { storageRoot } from "../vendor/lib/storage/encrypted-fs.js";
import { ACCOUNT_LIMITS } from "../vendor/lib/constants.js";

export const MAX_TRANSFER_BYTES = 10 * 1024 * 1024;
const derive = promisify(scrypt);
const FORMAT = "WaraqStudioAccounts";
const AAD = Buffer.from("WaraqStudioAccounts\0v1\0scrypt-131072-8-1\0aes-256-gcm\0");
const SCRYPT = Object.freeze({ N: 131072, r: 8, p: 1, maxmem: 256 * 1024 * 1024 });
const text = (max) => z.string().min(1).max(max);
const timestamp = z.number().int().nonnegative().safe();
const accountSchema = z.object({
  alias: text(80), enabled: z.boolean(), refreshToken: text(65536),
  accessToken: text(65536).optional(), expiresAt: timestamp.optional(),
  tokenRotatedAt: timestamp.optional(), oauthScope: text(4096).optional(),
  accountId: text(512).optional(), accountUserId: text(512).optional(),
  organizationId: text(512).optional(),
  accountIdSource: z.enum(["token", "id_token", "org", "manual"]).optional(),
  email: text(254).optional(), planType: text(80).optional(),
}).strict();
const payloadSchema = z.object({ version: z.literal(1), accounts: z.array(accountSchema).min(1).max(ACCOUNT_LIMITS.MAX_ACCOUNTS) }).strict();
const envelopeSchema = z.object({
  format: z.literal(FORMAT), version: z.literal(1),
  salt: text(24), nonce: text(16), tag: text(24), ciphertext: text(MAX_TRANSFER_BYTES),
}).strict();
const credentialFields = ["refreshToken", "accessToken", "expiresAt", "tokenRotatedAt", "oauthScope",
  "accountId", "accountUserId", "organizationId", "accountIdSource", "email", "planType"];
export function transferError(code) { return Object.assign(new Error(code), { safeCode: code }); }
export function validateTransferPassword(password) {
  if (typeof password !== "string" || !password.isWellFormed() || [...password].length < 12 || [...password].length > 256)
    throw transferError("TRANSFER_PASSWORD_INVALID");
}
function validatedPayload(value) {
  const parsed = payloadSchema.safeParse(value);
  if (!parsed.success) throw transferError("TRANSFER_FORMAT_INVALID");
  return parsed.data;
}
function decodeBase64(value, size) {
  if (value.length % 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) throw transferError("TRANSFER_FORMAT_INVALID");
  const bytes = Buffer.from(value, "base64");
  if ((size !== undefined && bytes.length !== size) || bytes.toString("base64") !== value) throw transferError("TRANSFER_FORMAT_INVALID");
  return bytes;
}
export async function encryptAccounts(payload, password) {
  validateTransferPassword(password);
  const plaintext = Buffer.from(JSON.stringify(validatedPayload(payload)), "utf8");
  const passwordBytes = Buffer.from(password, "utf8");
  const salt = randomBytes(16), nonce = randomBytes(12);
  let key;
  try {
    key = await derive(passwordBytes, salt, 32, SCRYPT);
    const cipher = createCipheriv("aes-256-gcm", key, nonce, { authTagLength: 16 });
    cipher.setAAD(Buffer.concat([AAD, salt, nonce]));
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const encoded = JSON.stringify({ format: FORMAT, version: 1, salt: salt.toString("base64"),
      nonce: nonce.toString("base64"), tag: cipher.getAuthTag().toString("base64"), ciphertext: ciphertext.toString("base64") });
    if (Buffer.byteLength(encoded) > MAX_TRANSFER_BYTES) throw transferError("TRANSFER_FILE_TOO_LARGE");
    return encoded;
  } finally { plaintext.fill(0); passwordBytes.fill(0); key?.fill(0); }
}
export async function decryptAccounts(encoded, password) {
  validateTransferPassword(password);
  if (Buffer.byteLength(encoded) > MAX_TRANSFER_BYTES) throw transferError("TRANSFER_FILE_TOO_LARGE");
  let value;
  try { value = JSON.parse(encoded); } catch { throw transferError("TRANSFER_FORMAT_INVALID"); }
  if (value?.format === FORMAT && value.version !== 1) throw transferError("TRANSFER_VERSION_UNSUPPORTED");
  const envelope = envelopeSchema.safeParse(value);
  if (!envelope.success) throw transferError("TRANSFER_FORMAT_INVALID");
  const { salt: saltText, nonce: nonceText, tag: tagText, ciphertext: ciphertextText } = envelope.data;
  const salt = decodeBase64(saltText, 16), nonce = decodeBase64(nonceText, 12), tag = decodeBase64(tagText, 16);
  const ciphertext = decodeBase64(ciphertextText);
  const passwordBytes = Buffer.from(password, "utf8");
  let key, first, last, plaintext;
  try {
    key = await derive(passwordBytes, salt, 32, SCRYPT);
    const decipher = createDecipheriv("aes-256-gcm", key, nonce, { authTagLength: 16 });
    decipher.setAAD(Buffer.concat([AAD, salt, nonce]));
    decipher.setAuthTag(tag);
    try {
      first = decipher.update(ciphertext);
      last = decipher.final(); // Never parse or persist unauthenticated output.
      plaintext = Buffer.concat([first, last]);
    } catch { throw transferError("TRANSFER_DECRYPT_FAILED"); }
    try { value = JSON.parse(plaintext.toString("utf8")); } catch { throw transferError("TRANSFER_FORMAT_INVALID"); }
    return validatedPayload(value);
  } finally { passwordBytes.fill(0); key?.fill(0); first?.fill(0); last?.fill(0); plaintext?.fill(0); }
}
function contained(root, path) {
  const rel = relative(root.toLowerCase(), path.toLowerCase());
  return rel === "" || (!isAbsolute(rel) && rel !== ".." && !rel.startsWith("..\\") && !rel.startsWith("../"));
}
async function canonicalPath(path) {
  const tail = [];
  let current = resolve(path);
  for (;;) {
    try { return join(await realpath(current), ...tail); }
    catch (e) {
      if (e.code !== "ENOENT") throw e;
      const parent = dirname(current);
      if (parent === current) throw e;
      tail.unshift(basename(current)); current = parent;
    }
  }
}
export async function transferPath(path) {
  if (typeof path !== "string" || !isAbsolute(path) || path.includes("\0") || extname(path).toLowerCase() !== ".waraq-accounts") throw transferError("TRANSFER_PATH_INVALID");
  const resolved = resolve(path);
  const physical = await canonicalPath(resolved), root = await canonicalPath(storageRoot);
  if (contained(root, physical) || contained(resolve(storageRoot), resolved)) throw transferError("TRANSFER_PATH_INVALID");
  try { if (!(await lstat(resolved)).isFile()) throw transferError("TRANSFER_PATH_INVALID"); }
  catch (e) { if (e.code !== "ENOENT") throw e; }
  return resolved;
}
async function readBounded(path) {
  const handle = await open(path, "r");
  try {
    const info = await handle.stat();
    if (!info.isFile()) throw transferError("TRANSFER_PATH_INVALID");
    if (info.size > MAX_TRANSFER_BYTES) throw transferError("TRANSFER_FILE_TOO_LARGE");
    const bytes = Buffer.alloc(MAX_TRANSFER_BYTES + 1);
    let length = 0;
    while (length < bytes.length) {
      const result = await handle.read(bytes, length, bytes.length - length, null);
      if (!result.bytesRead) break;
      length += result.bytesRead;
    }
    if (length > MAX_TRANSFER_BYTES) throw transferError("TRANSFER_FILE_TOO_LARGE");
    return bytes.subarray(0, length).toString("utf8");
  } finally { await handle.close(); }
}
export async function exportAccountsFile(input, { accountRef, rows }) {
  validateTransferPassword(input.password);
  const path = await transferPath(input.filePath);
  const selected = new Set(input.accountRefs);
  if (!selected.size || selected.size !== input.accountRefs.length || selected.size > ACCOUNT_LIMITS.MAX_ACCOUNTS) throw transferError("TRANSFER_SELECTION_INVALID");
  const accounts = await withAccountStorageTransaction(async pool => {
    const result = (pool?.accounts || []).filter(a => selected.has(accountRef(a))).map(a => {
      const row = rows[accountRef(a)] || {};
      const result = { alias: (row.alias || a.accountLabel || "Account").slice(0, 80), enabled: row.enabled !== false && a.enabled !== false };
      for (const key of credentialFields) if (a[key] !== undefined && a[key] !== "") result[key] = a[key];
      return result;
    });
    if (result.length !== selected.size || result.some(a => !a.refreshToken)) throw transferError("TRANSFER_SELECTION_INVALID");
    return result;
  });
  const encoded = await encryptAccounts({ version: 1, accounts }, input.password);
  await writeFileAtomic(path, encoded);
  return { exported: accounts.length };
}
export async function importAccountsFile(input) {
  validateTransferPassword(input.password);
  const path = await transferPath(input.filePath);
  const payload = await decryptAccounts(await readBounded(path), input.password);
  return withAccountStorageTransaction(async (current, persist) => {
    const existing = current?.accounts || [], accounts = [...existing];
    let skipped = 0;
    for (const entry of payload.accounts) {
      const { alias, ...credentials } = entry;
      const candidate = { ...credentials, accountLabel: alias, addedAt: Date.now(), lastUsed: 0 };
      // Run the storage equivalence rules on throwaway records, never their
      // "newest wins" merge on the actual local credentials.
      if (accounts.some(a => deduplicateAccountsForStorage([{ ...a }, { ...candidate }]).length < 2)) { skipped++; continue; }
      accounts.push(candidate);
    }
    if (accounts.length > ACCOUNT_LIMITS.MAX_ACCOUNTS) throw transferError("TRANSFER_LIMIT_EXCEEDED");
    const imported = accounts.length - existing.length;
    if (imported) await persist({ ...(current || { version: 3, activeIndex: 0, activeIndexByFamily: {} }), accounts });
    return { imported, skipped, total: accounts.length };
  });
}
