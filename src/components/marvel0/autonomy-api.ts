// Typed client of the autonomy system's routes (`/api/levi/marvel0/aeri...`,
// design section 8: 12 routes, two routers). Same origin: the proxy adds the
// person's token, nothing here reads one. Nothing here talks to a robot-side
// port. A write carries a `request_id` the caller keeps across a retry, and
// the bodies are closed: there is no field for a raw byte, only the named
// actions of autonomy-doc.ts (`KEY_IDS`), and the byte a key stands for is
// chosen by the backend's own table.
import { createCaller, type ApiResult } from "./deploy-api";
import { MARVEL0_REQUEST_MS } from "./marvel0-logic";
import type { KeyId } from "./autonomy-doc";

export interface AutonomyDeadlines {
  read: number;
  /** Prepare runs a read-only check of the AERI code (up to 60 s). */
  prepare: number;
  /** A start waits for the checks and the launch. */
  start: number;
  /** A key, a stop, an end or a presence beat is answered at once or not at
   * all. */
  write: number;
  /** Stopping the live service or the leftover units waits for systemd. */
  stopUnits: number;
}
export const AUTONOMY_DEADLINES: AutonomyDeadlines = {
  read: MARVEL0_REQUEST_MS,
  prepare: 90_000,
  start: 60_000,
  write: 15_000,
  stopUnits: 150_000,
};

export interface AutonomyApi {
  ready(signal?: AbortSignal): Promise<ApiResult>;
  prepare(body: unknown): Promise<ApiResult>;
  liveStop(body: unknown): Promise<ApiResult>;
  leftoversStop(body: unknown): Promise<ApiResult>;
  run(signal?: AbortSignal): Promise<ApiResult>;
  challenge(body: unknown): Promise<ApiResult>;
  start(body: unknown): Promise<ApiResult>;
  key(body: unknown): Promise<ApiResult>;
  stop(body: unknown): Promise<ApiResult>;
  end(body: unknown): Promise<ApiResult>;
  presence(body: unknown): Promise<ApiResult>;
  log(lines: number, signal?: AbortSignal): Promise<ApiResult>;
}

export function createAutonomyApi({
  fetchFn,
  deadlines = {},
}: {
  fetchFn?: (input: string, init?: RequestInit) => Promise<Response>;
  deadlines?: Partial<AutonomyDeadlines>;
} = {}): AutonomyApi {
  const limit = { ...AUTONOMY_DEADLINES, ...deadlines };
  const call = createCaller({ fetchFn });
  return {
    ready: (signal) => call("aeri", limit.read, undefined, signal),
    prepare: (body) => call("aeri/prepare", limit.prepare, body),
    liveStop: (body) => call("aeri/live/stop", limit.stopUnits, body),
    leftoversStop: (body) => call("aeri/leftovers/stop", limit.stopUnits, body),
    run: (signal) => call("aeri/run", limit.read, undefined, signal),
    challenge: (body) => call("aeri/run/challenge", limit.write, body),
    start: (body) => call("aeri/run/start", limit.start, body),
    key: (body) => call("aeri/run/key", limit.write, body),
    stop: (body) => call("aeri/run/stop", limit.write, body),
    end: (body) => call("aeri/run/end", limit.write, body),
    presence: (body) => call("aeri/run/presence", limit.write, body),
    log: (lines, signal) =>
      call(
        `aeri/run/log?lines=${Math.min(200, Math.max(1, Math.trunc(lines)))}`,
        limit.read,
        undefined,
        signal,
      ),
  };
}

/** The client the page uses. */
export const autonomyApi: AutonomyApi = createAutonomyApi();

// -- request bodies: closed, as the backend's models are (`extra="forbid"`)

/** The only purpose a challenge is asked for: the keys carry their own token
 * in the document. */
export const challengeBody = (name: string): Record<string, unknown> => ({
  purpose: "start",
  name,
});

export interface StartAttest {
  estop: boolean;
  scene_ok: boolean;
  area_clear: boolean;
  arm_home: boolean;
}

export const startBody = (
  requestId: string,
  name: string,
  challenge: string,
  confirm: string,
  attest: StartAttest,
): Record<string, unknown> => ({
  request_id: requestId,
  name,
  challenge,
  confirm,
  attest,
});

/** What the page of the key was looking at when the person clicked: the
 * backend compares it with its own state now, and sends nothing when they
 * differ. */
export interface Seen {
  run_id: string;
  state_seq: number;
  /** Always a string: a key answers a prompt (the model is strict). */
  prompt_id: string;
}

/** `attest` is the codes of the ticks the person set in the key's window, the
 * very codes of that key's required ticks (`requiredAttest`); sent for the keys
 * that have ticks (`execute`, `execute_variant`, `release_round`) and for the
 * forced end, and for no other key. The backend holds the same table and
 * refuses a request without them (review N6: until then the ticks were only
 * checked in the browser). */
export const keyBody = (
  requestId: string,
  action: KeyId,
  seen: Seen,
  token: string,
  tabId: string,
  confirm?: string,
  attest?: readonly string[],
): Record<string, unknown> => ({
  request_id: requestId,
  action,
  seen,
  token,
  tab_id: tabId,
  ...(confirm ? { confirm } : {}),
  ...(attest && attest.length > 0 ? { attest: [...attest] } : {}),
});

/** A stop has no `seen` and no token: it must go whatever the page's view. */
export const stopBody = (requestId: string): Record<string, unknown> => ({
  request_id: requestId,
});

export const endBody = (
  requestId: string,
  confirm: string,
  attest?: readonly string[],
): Record<string, unknown> => ({
  request_id: requestId,
  confirm,
  ...(attest && attest.length > 0 ? { attest: [...attest] } : {}),
});

export const presenceBody = (
  tabId: string,
  visible: boolean,
): Record<string, unknown> => ({ tab_id: tabId, visible });

export const confirmBody = (
  requestId: string,
  confirm: string,
): Record<string, unknown> => ({ request_id: requestId, confirm });
