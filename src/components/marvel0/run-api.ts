// Typed client of the hosted client's routes (`/api/levi/marvel0/run...`,
// levi/marvel0/run_api.py): the state, a one-time challenge, a start, a key
// (an action of the client's gates), a hold, an end and the log. Same origin:
// the proxy adds the person's token, nothing here reads one. A write carries a
// `request_id` the caller keeps across a retry. Nothing here talks to a
// robot-side port, and no key but the named actions can be sent: there is no
// field for a raw key.
import { createCaller, type ApiResult } from "./deploy-api";
import { MARVEL0_REQUEST_MS } from "./marvel0-logic";
import type { ActionId, Purpose } from "./run-logic";

export interface RunDeadlines {
  read: number;
  /** A start waits for the checks and the launch. */
  start: number;
  /** A key, a hold or an end is answered at once or not at all. */
  write: number;
}
export const RUN_DEADLINES: RunDeadlines = {
  read: MARVEL0_REQUEST_MS,
  start: 60_000,
  write: 15_000,
};

/** The actions that send a key of the client's gates. */
export type KeyAction = Extract<
  ActionId,
  "enter" | "success" | "failure" | "discard" | "end_at_gate" | "exit_home"
>;

export interface RunApi {
  read(signal?: AbortSignal): Promise<ApiResult>;
  challenge(body: unknown): Promise<ApiResult>;
  start(body: unknown): Promise<ApiResult>;
  key(body: unknown): Promise<ApiResult>;
  hold(body: unknown): Promise<ApiResult>;
  end(body: unknown): Promise<ApiResult>;
  log(signal?: AbortSignal): Promise<ApiResult>;
}

export function createRunApi({
  fetchFn,
  deadlines = {},
}: {
  fetchFn?: (input: string, init?: RequestInit) => Promise<Response>;
  deadlines?: Partial<RunDeadlines>;
} = {}): RunApi {
  const limit = { ...RUN_DEADLINES, ...deadlines };
  const call = createCaller({ fetchFn });
  return {
    read: (signal) => call("run", limit.read, undefined, signal),
    challenge: (body) => call("run/challenge", limit.write, body),
    start: (body) => call("run/start", limit.start, body),
    key: (body) => call("run/key", limit.write, body),
    hold: (body) => call("run/hold", limit.write, body),
    end: (body) => call("run/end", limit.write, body),
    log: (signal) => call("run/log", limit.read, undefined, signal),
  };
}

/** The client the page uses. */
export const runApi: RunApi = createRunApi();

// -- request bodies

// The backend's body models are closed (levi/marvel0/run_api.py: unknown or
// missing fields are a 422). A challenge has no request id: it is a token,
// and asking again just replaces it.
export const challengeBody = (
  purpose: Purpose,
  form?: Record<string, unknown>,
): Record<string, unknown> => ({
  purpose,
  ...(form ? { form } : {}),
});

export const startRunBody = (
  requestId: string,
  form: Record<string, unknown>,
  challenge: string,
  confirm: string,
  attest: { estop: boolean; scene_reset: boolean; area_clear: boolean },
): Record<string, unknown> => ({
  request_id: requestId,
  form,
  challenge,
  confirm,
  attest,
});

export const keyBody = (
  requestId: string,
  action: KeyAction,
  challenge: string,
  confirm?: string,
): Record<string, unknown> => ({
  request_id: requestId,
  action,
  challenge,
  ...(confirm ? { confirm } : {}),
});

export const holdBody = (requestId: string): Record<string, unknown> => ({
  request_id: requestId,
});

export const endBody = (
  requestId: string,
  confirm: string,
): Record<string, unknown> => ({ request_id: requestId, confirm });
