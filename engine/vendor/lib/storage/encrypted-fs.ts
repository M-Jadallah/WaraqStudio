/** Waraq patch: all credential-bearing filesystem bytes are DPAPI protected before
 * any temp file is opened. Rename/fsync/locks remain upstream's responsibility. */
import * as native from "node:fs/promises";
import { existsSync, realpathSync } from "node:fs";
import { resolve, relative, isAbsolute } from "node:path";
import { Dpapi, isPlatformSupported } from "@primno/dpapi";
export { existsSync, realpathSync };
export const storageRoot = resolve(process.env.WARAQ_ACCOUNTS_STORAGE_DIR || (() => { throw new Error("PRIVATE_STORAGE_REQUIRED"); })());
const magic = Buffer.from("WARAQ-DPAPI-1\n");
const entropy = Buffer.from("WaraqStudio/accounts/v1");
function protectedPath(path: unknown): boolean {
  if (typeof path !== "string") throw new Error("INVALID_STORAGE_PATH");
  const rel = relative(storageRoot, resolve(path));
  return !isAbsolute(rel) && rel !== ".." && !rel.startsWith("..\\") && !rel.startsWith("../");
}
export function seal(content: string | Uint8Array): Buffer {
  if (!isPlatformSupported) throw new Error("DPAPI_UNAVAILABLE");
  return Buffer.concat([magic, Buffer.from(Dpapi.protectData(Buffer.from(content), entropy, "CurrentUser"))]);
}
export function unseal(content: Uint8Array): Buffer {
  const bytes = Buffer.from(content);
  if (!bytes.subarray(0, magic.length).equals(magic)) throw new Error("PLAINTEXT_STORAGE_REFUSED");
  if (!isPlatformSupported) throw new Error("DPAPI_UNAVAILABLE");
  try { return Buffer.from(Dpapi.unprotectData(bytes.subarray(magic.length), entropy, "CurrentUser")); }
  catch { throw new Error("DPAPI_DECRYPT_FAILED"); }
}
const readFile = async (path: any, options?: any): Promise<any> => {
  if (!protectedPath(path)) return native.readFile(path, options);
  const content = unseal(await native.readFile(path));
  const encoding = typeof options === "string" ? options : options?.encoding;
  return encoding ? content.toString(encoding) : content;
};
const writeFile = async (path: any, content: any, options?: any): Promise<void> => {
  await native.writeFile(path, protectedPath(path) ? seal(content) : content, options);
};
const open = async (path: any, flags: any, mode?: any): Promise<any> => {
  const handle = await native.open(path, flags, mode);
  if (!protectedPath(path)) return handle;
  return new Proxy(handle, { get(target, key) {
    if (key === "writeFile") return async (content: any) => target.writeFile(seal(content));
    const value = Reflect.get(target, key, target);
    return typeof value === "function" ? value.bind(target) : value;
  }});
};
export { readFile, writeFile, open };
export const promises = { ...native, readFile, writeFile, open } as typeof native;
export const { readdir, rm, mkdir } = native;
