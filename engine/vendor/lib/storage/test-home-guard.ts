/**
 * The test-run write guard, shared by every storage writer.
 *
 * Extracted from `lib/storage/load-save.ts` so the credential-snapshot writer
 * can apply the same check. Importing it from `load-save.ts` directly would be
 * a cycle: `load-save.ts` is what triggers snapshots in the first place.
 */

import os from "node:os";
import { StorageError } from "./errors.js";
import { isWithinDirectory } from "./paths.js";

/**
 * StorageError code for a write refused inside the developer's real home.
 *
 * Shared rather than spelled out at each site: several callers deliberately
 * absorb storage failures and have to re-throw this one, and a typo in any of
 * those copies would silently turn the guard back into a swallowed warning.
 */
export const TEST_HOME_ESCAPE_CODE = "TEST_HOME_ESCAPE";

/**
 * Refuse to touch account storage inside the developer's real home while the
 * test suite is running.
 *
 * `vitest.config.ts` redirects HOME to a sandbox before any module loads, but a
 * test that restores the captured real HOME, or a future regression in that
 * config, would otherwise read or overwrite live ChatGPT credentials.
 * `os.userInfo()` reads the passwd entry instead of `$HOME`, so it still names
 * the real home after the redirect and gives the check something the sandbox
 * cannot spoof. Inert outside vitest.
 *
 * The sandbox and the temp directory are exempt, and both exemptions are
 * load-bearing rather than convenience: on Windows `os.tmpdir()` normally sits
 * inside the user profile, so there every legitimate sandbox path is also a
 * real-home path and a bare home-prefix test would reject the entire suite.
 * Neither exemption can reach the production store, which lives under the real
 * home's `.opencode`.
 */
export function assertTestRunNeverTouchesRealHome(path: string): void {
  if (!process.env.VITEST) return;

  let realHome: string;
  try {
    realHome = os.userInfo().homedir;
  } catch {
    return;
  }
  if (!realHome || !isWithinDirectory(realHome, path)) return;

  // A root exempts what lies beneath it only while it does not itself swallow
  // the real home. Without that clause `OC_CODEX_TEST_HOME=/` or `TMPDIR=$HOME`
  // would disarm the guard completely.
  const exempts = (root: string | undefined): boolean =>
    !!root && !isWithinDirectory(root, realHome) && isWithinDirectory(root, path);

  if (exempts(process.env.OC_CODEX_TEST_HOME)) return;
  if (exempts(os.tmpdir())) return;

  throw new StorageError(
    `Refusing to touch account storage inside the real home directory during a test run: ${path}`,
    TEST_HOME_ESCAPE_CODE,
    path,
    "A test resolved account storage against the developer's real home. Point HOME at a temp directory for the whole vitest process (see vitest.config.ts) instead of overriding it per test.",
  );
}
