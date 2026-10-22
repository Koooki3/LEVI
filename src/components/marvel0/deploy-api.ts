// Typed client of the marvel0's action routes (`/api/levi/marvel0/models`,
// `site`, `command/preview`, `deploy`, `deploy/stop`, `deploy/log`). Same
// origin: the product frontend's proxy adds the person's token, so nothing
// here reads, stores or shows one. A write carries a `request_id` the caller
// keeps across a retry. Nothing here talks to a robot-side port.
import { MARVEL0_REQUEST_MS } from "./marvel0-logic";
import { rec, str } from "./marvel0-json";

const BASE = "/api/levi/marvel0";

/** A call that did not succeed. `status` is 0 when no answer came at all
 * (code `network`); `code` is the server's stable name for the refusal, FastAPI's
 * `validation` for a body it did not accept, or `http_<status>`. */
export interface ApiFailure {
  ok: false;
  status: number;
  code: string;
  /** The server's own sentence, in English whatever the page's language. */
  message: string;
  /** The facts of the refusal (everything but code, message and field). */
  extra: Record<string, unknown>;
  /** The form field a refusal is about, when it names one. */
  field: string | null;
  /** FastAPI's per-field errors of a body it did not accept. */
  fields: Array<{ field: string; message: string }>;
}

export type ApiResult<T = unknown> =
  | { ok: true; status: number; data: T }
  | ApiFailure;

/** Reads an error answer: LEVI's `{detail: {code, message, ...}}`, FastAPI's
 * `{detail: [{loc, msg}, ...]}`, a plain `{detail: "text"}`, or nothing usable
 * (a proxy's page), which keeps the status line. */
export function errorOf(status: number, body: unknown): ApiFailure {
  const detail = rec(body)?.detail;
  const fields: ApiFailure["fields"] = [];
  let code = "";
  let message = "";
  let field: string | null = null;
  let extra: Record<string, unknown> = {};
  const inner = rec(detail);
  if (inner) {
    code = typeof inner.code === "string" ? inner.code : "";
    message = typeof inner.message === "string" ? inner.message : "";
    field = str(inner.field, 80);
    extra = Object.fromEntries(
      Object.entries(inner).filter(
        ([key]) => key !== "code" && key !== "message" && key !== "field",
      ),
    );
  } else if (Array.isArray(detail)) {
    for (const entry of detail) {
      const item = rec(entry);
      const loc = Array.isArray(item?.loc) ? item.loc : [];
      const name = loc[0] === "body" && loc.length > 1 ? String(loc[1]) : null;
      const text = str(item?.msg);
      if (name && text) fields.push({ field: name, message: text });
      else if (text && !message) message = text;
    }
    code = "validation";
    field = fields[0]?.field ?? null;
    message = fields[0]?.message ?? message;
  } else if (typeof detail === "string") message = detail;
  return {
    ok: false,
    status,
    code: code || `http_${status}`,
    message: message || `HTTP ${status}`,
    extra,
    field,
    fields,
  };
}

const noAnswer = (message: string): ApiFailure => ({
  ok: false,
  status: 0,
  code: "network",
  message,
  extra: {},
  field: null,
  fields: [],
});

/** How long each kind of request may stay unanswered. A deploy waits for the
 * server's own checks and `systemd-run` (15 s); a stop waits for the unit to
 * go away (up to its stop timeout plus 30 s); a preview only renders text. A
 * write that is given up may still have been accepted: its request id is kept,
 * so sending it again is answered, never done twice. */
export interface Deadlines {
  read: number;
  deploy: number;
  stop: number;
  preview: number;
}
export const DEADLINES: Deadlines = {
  read: MARVEL0_REQUEST_MS,
  deploy: 90_000,
  stop: 240_000,
  preview: 30_000,
};

/** A body sent as it is, with the Blob's own type as its Content-Type (a
 * recorded clip); anything else is sent as JSON. */
export interface RawBody {
  raw: Blob;
}
const isRaw = (body: unknown): body is RawBody =>
  typeof body === "object" &&
  body !== null &&
  (body as RawBody).raw instanceof Blob;

export interface Marvel0Api {
  models(signal?: AbortSignal): Promise<ApiResult>;
  site(signal?: AbortSignal): Promise<ApiResult>;
  deploy(signal?: AbortSignal): Promise<ApiResult>;
  log(lines: number, signal?: AbortSignal): Promise<ApiResult>;
  startDeploy(body: unknown): Promise<ApiResult>;
  stopDeploy(body: unknown): Promise<ApiResult>;
  preview(body: unknown): Promise<ApiResult>;
}

type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

/** One request of the marvel0's routes: `call(path, deadline, body?, signal?)`
 * with a path under `/api/levi/marvel0/`. A body makes it a POST. */
export type Caller = (
  path: string,
  deadline: number,
  body?: unknown,
  signal?: AbortSignal,
) => Promise<ApiResult>;

/** The request machinery every client of the marvel0's routes shares (this
 * file's, the arm stack's and the run's): same origin, a hard deadline, and
 * one `ApiResult` for every outcome. */
export function createCaller({
  // Late-bound, so a test that replaces globalThis.fetch is seen.
  fetchFn = (input, init) => globalThis.fetch(input, init),
}: {
  fetchFn?: FetchFn;
} = {}): Caller {
  /** One request with a deadline. A race, not only an abort of the request, so
   * one that ignores its signal (or a body that never ends) cannot hold the
   * caller either. `signal` is the caller's: when it aborts, this throws. */
  return async function call(
    path: string,
    deadline: number,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<ApiResult> {
    const request = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let dropped = () => {};
    const cut = new Promise<never>((_, reject) => {
      const giveUp = (why: string) => {
        reject(new Error(why));
        request.abort();
      };
      dropped = () => giveUp("aborted");
      timer = setTimeout(
        () => giveUp(`no answer within ${Math.round(deadline / 1000)} s`),
        deadline,
      );
    });
    if (signal?.aborted) dropped();
    else signal?.addEventListener("abort", dropped, { once: true });
    try {
      const response = await Promise.race([
        fetchFn(`${BASE}/${path}`, {
          cache: "no-store",
          signal: request.signal,
          ...(body === undefined
            ? {}
            : isRaw(body)
              ? {
                  method: "POST",
                  headers: { "Content-Type": body.raw.type },
                  body: body.raw,
                }
              : {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(body),
                }),
        }),
        cut,
      ]);
      const data: unknown = await Promise.race([
        response.json().catch(() => null),
        cut,
      ]);
      if (!response.ok) return errorOf(response.status, data);
      return { ok: true, status: response.status, data };
    } catch (error) {
      if (signal?.aborted) throw error;
      return noAnswer(error instanceof Error ? error.message : String(error));
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", dropped);
    }
  };
}

export function createMarvel0Api({
  fetchFn,
  deadlines = {},
}: {
  fetchFn?: FetchFn;
  deadlines?: Partial<Deadlines>;
} = {}): Marvel0Api {
  const limit = { ...DEADLINES, ...deadlines };
  const call = createCaller({ fetchFn });

  return {
    models: (signal) => call("models", limit.read, undefined, signal),
    site: (signal) => call("site", limit.read, undefined, signal),
    deploy: (signal) => call("deploy", limit.read, undefined, signal),
    log: (lines, signal) =>
      call(
        `deploy/log?lines=${Math.trunc(lines)}`,
        limit.read,
        undefined,
        signal,
      ),
    startDeploy: (body) => call("deploy", limit.deploy, body),
    stopDeploy: (body) => call("deploy/stop", limit.stop, body),
    preview: (body) => call("command/preview", limit.preview, body),
  };
}

/** The client the pages use. */
export const marvel0Api: Marvel0Api = createMarvel0Api();
