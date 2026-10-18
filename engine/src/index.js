// Capture and erase the startup-only capability before importing upstream code.
// OpenCode may reload module graphs for another task location in the same
// plugin host. Keep the startup secret in a private, non-enumerable host slot.
const key = Symbol.for("WaraqStudio.accounts.capability.v1");
const capability = globalThis[key] || process.env.WARAQ_ACCOUNTS_CAPABILITY;
delete process.env.WARAQ_ACCOUNTS_CAPABILITY;
if (typeof capability !== "string" || capability.length < 32) throw new Error("CAPABILITY_REQUIRED");
if (!globalThis[key]) Object.defineProperty(globalThis,key,{value:capability});
const { createAccountsRpc } = await import("./rpc.js");
const { setupV2 } = await import("../vendor/lib/opencode-v2.js");
const { createPluginRuntime } = await import("../vendor/index.js");
export default {
  id: "waraq-accounts",
  async setup(context) {
    const registration = await createAccountsRpc(context, capability);
    let dispose;
    try { dispose = await setupV2(context, createPluginRuntime); }
    catch (error) { await registration.dispose(); throw error; }
    return async () => { await registration.dispose(); await dispose?.(); };
  },
};
