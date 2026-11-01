// Typed client of the hosted client's routes (`/api/levi/marvel0/run...`,
// levi/marvel0/run_api.py): the state, a one-time challenge, a start, a key
// (an action of the client's gates), a hold, an end and the log. Same origin:
// the proxy adds the person's token, nothing here reads one. A write carries a
// `request_id` the caller keeps across a retry. Nothing here talks to a
// robot-side port, and no key but the named actions can be sent: there is no
// field for a raw key.
import { BASE, createCaller, type ApiResult } from "./deploy-api";
import { rec } from "./marvel0-json";
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

/** The cameras of the live view (`GET .../run/frame?camera=`). */
export const FRAME_CAMERAS = ["side", "wrist"] as const;
export type FrameCamera = (typeof FRAME_CAMERAS)[number];

/** Why there is no picture: the backend's three reasons (404), or
 * `unavailable` for anything else (no answer, a body that is no picture). */
export type FrameReason = "no_run" | "no_episode" | "no_frame" | "unavailable";
const FRAME_REASONS: readonly string[] = ["no_run", "no_episode", "no_frame"];

export type FrameResult =
  | {
      ok: true;
      blob: Blob;
      /** How long ago the recording file was last written (ms), when stated. */
      ageMs: number | null;
      /** The episode folder's name (`demo_0003`), when stated. */
      episode: string | null;
    }
  | { ok: false; reason: FrameReason };

export interface RunApi {
  read(signal?: AbortSignal): Promise<ApiResult>;
  challenge(body: unknown): Promise<ApiResult>;
  start(body: unknown): Promise<ApiResult>;
  key(body: unknown): Promise<ApiResult>;
  hold(body: unknown): Promise<ApiResult>;
  end(body: unknown): Promise<ApiResult>;
  log(signal?: AbortSignal): Promise<ApiResult>;
  /** The newest frame of one camera of the episode being recorded. Resolves
   * (never rejects) unless `signal` aborts it. */
  frame(camera: FrameCamera, signal?: AbortSignal): Promise<FrameResult>;
}

/** A frame is small and local; an answer later than this is given up, so one
 * stuck request cannot hold the live view's one-in-flight rule forever. */
export const FRAME_DEADLINE_MS = 5_000;

const headerNumber = (value: string | null): number | null => {
  if (value === null || !/^\d{1,9}$/.test(value)) return null;
  return Number(value);
};
const headerName = (value: string | null): string | null =>
  value !== null && /^[\w.-]{1,80}$/.test(value) ? value : null;

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
    frame: async (camera, signal) => {
      const request = new AbortController();
      let timer: ReturnType<typeof setTimeout> | undefined;
      let dropped = () => {};
      // A race as well as an abort, so a fetch that ignores its signal cannot
      // hold the caller either.
      const cut = new Promise<never>((_, reject) => {
        const giveUp = (why: string) => {
          reject(new Error(why));
          request.abort();
        };
        dropped = () => giveUp("aborted");
        timer = setTimeout(() => giveUp("no answer"), FRAME_DEADLINE_MS);
      });
      cut.catch(() => {});
      if (signal?.aborted) dropped();
      else signal?.addEventListener("abort", dropped, { once: true });
      try {
        const response = await Promise.race([
          (fetchFn ?? ((input, init) => globalThis.fetch(input, init)))(
            `${BASE}/run/frame?camera=${camera}`,
            { cache: "no-store", signal: request.signal },
          ),
          cut,
        ]);
        if (response.ok) {
          const blob = await Promise.race([response.blob(), cut]);
          if (!blob.type.startsWith("image/") || blob.size === 0)
            return { ok: false, reason: "unavailable" };
          return {
            ok: true,
            blob,
            ageMs: headerNumber(response.headers.get("x-levi-frame-age-ms")),
            episode: headerName(response.headers.get("x-levi-frame-episode")),
          };
        }
        const code = rec(
          rec(await Promise.race([response.json().catch(() => null), cut]))
            ?.detail,
        )?.code;
        return {
          ok: false,
          reason:
            response.status === 404 &&
            typeof code === "string" &&
            FRAME_REASONS.includes(code)
              ? (code as FrameReason)
              : "unavailable",
        };
      } catch {
        if (signal?.aborted) throw new Error("aborted");
        return { ok: false, reason: "unavailable" };
      } finally {
        clearTimeout(timer);
        signal?.removeEventListener("abort", dropped);
      }
    },
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
