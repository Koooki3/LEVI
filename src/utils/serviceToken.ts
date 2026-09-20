// Server-only: the credential the loopback LEVI API expects from the web UI.
import { readFileSync } from "node:fs";
import { join } from "node:path";

let cached: { value: string | undefined; at: number } | null = null;
const TTL_MS = 2000;

/** The current UI token, read from the file the service owns.
 *
 * The token lives in the workspace and is rewritten whenever the shared Agent
 * Core starts. A value captured into this process's environment at launch goes
 * stale the moment that Core is restarted underneath it, and every local
 * dataset then answers 401 while looking like a permission problem. Reading the
 * file keeps the proxy correct across restarts; the environment stays as the
 * fallback for deployments that inject the token directly.
 */
export function uiToken(): string | undefined {
  const now = Date.now();
  if (cached && now - cached.at < TTL_MS) return cached.value;
  let value: string | undefined;
  const directory = process.env.LEVI_CORE_DIR;
  if (directory) {
    try {
      value =
        readFileSync(join(directory, "human.key"), "utf8").trim() || undefined;
    } catch {
      value = undefined;
    }
  }
  value = value || process.env.LEVI_UI_TOKEN || undefined;
  cached = { value, at: now };
  return value;
}

/** Testing seam: the 2 s memo would otherwise outlive a test's fixture. */
export function forgetUiToken(): void {
  cached = null;
}
