// Typed client for the automatic-evaluation API (contract section 2). All
// requests go to the same-origin proxy `/api/levi/automatic/...`, which adds
// the person's token: this file never reads, stores or shows one. Writes
// carry a `request_id` or `command_id` the caller keeps across a retry.
import type {
  CommandResult,
  EventsResponse,
  JobsResponse,
  LabelResult,
  LaunchPlan,
  MetricsReport,
  OperatorLabel,
  RunSnapshot,
  RunSummary,
} from "./types";

const BASE = "/api/levi/automatic";

/** A failed call: HTTP status and the contract's `E_...` style code. */
export class AutomaticApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "AutomaticApiError";
    this.status = status;
    this.code = code;
  }
}

async function failure(response: Response): Promise<AutomaticApiError> {
  let code = "";
  let message = `HTTP ${response.status}`;
  try {
    const data = await response.json();
    // {"detail": {"code", "message"}} (the live service's shape), the older
    // {"error": {...}}, a refused command {"result", "code"}, or FastAPI's
    // plain {"detail": "text"}.
    const body =
      data?.detail &&
      typeof data.detail === "object" &&
      !Array.isArray(data.detail)
        ? data.detail
        : data?.error && typeof data.error === "object"
          ? data.error
          : typeof data?.code === "string"
            ? data
            : null;
    if (body) {
      code = String(body.code ?? "");
      message = String(body.message ?? body.code ?? message);
    } else if (typeof data?.detail === "string") {
      message = data.detail;
    }
  } catch {
    /* the body was not JSON: keep the status line */
  }
  return new AutomaticApiError(response.status, code, message);
}

async function call<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${BASE}/${path}`, {
    method,
    cache: "no-store",
    signal,
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

/** A fresh id for one attempt of a write; reuse it for a retry of the same attempt. */
export function newRequestId(): string {
  return globalThis.crypto.randomUUID();
}

const run = (id: string) => `runs/${encodeURIComponent(id)}`;

export const automaticApi = {
  jobs: (signal?: AbortSignal) =>
    call<JobsResponse>("GET", "jobs", undefined, signal),
  plan: (jobId: string, executionMode?: string) =>
    call<LaunchPlan>("POST", "plan", {
      job_id: jobId,
      ...(executionMode ? { execution_mode: executionMode } : {}),
    }),
  launch: (plan: LaunchPlan, jobId: string, requestId: string) =>
    call<{ run_id: string }>("POST", "runs", {
      job_id: jobId,
      plan_sha256: plan.plan_sha256,
      launch_token: plan.launch_token,
      confirm: "launch",
      request_id: requestId,
    }),
  runs: (signal?: AbortSignal) =>
    call<{ runs: RunSummary[] }>("GET", "runs", undefined, signal),
  snapshot: (id: string, signal?: AbortSignal) =>
    call<RunSnapshot>("GET", run(id), undefined, signal),
  events: (id: string, after: number, signal?: AbortSignal) =>
    call<EventsResponse>(
      "GET",
      `${run(id)}/events?after=${after}&limit=200`,
      undefined,
      signal,
    ),
  metrics: (id: string, signal?: AbortSignal) =>
    call<MetricsReport>("GET", `${run(id)}/metrics`, undefined, signal),
  stop: (id: string, commandId: string) =>
    call<CommandResult>("POST", `${run(id)}/stop`, {
      command_id: commandId,
      confirm: "stop",
    }),
  resume: (
    id: string,
    body: {
      commandId: string;
      expectedSeq: number;
      environmentHandled: boolean;
      healthRechecked: boolean;
      challenge: string;
    },
  ) =>
    call<CommandResult>("POST", `${run(id)}/resume`, {
      command_id: body.commandId,
      expected_seq: body.expectedSeq,
      environment_handled: body.environmentHandled,
      health_rechecked: body.healthRechecked,
      challenge: body.challenge,
    }),
  label: (id: string, episodeId: string, value: OperatorLabel) =>
    call<LabelResult>("POST", `${run(id)}/labels`, {
      episode_id: episodeId,
      value,
    }),
  sceneAnswer: (
    id: string,
    question: { request_id: string; nonce: string; frames_sha256: string },
    predicates: Record<string, boolean | null>,
  ) =>
    call<unknown>("POST", `${run(id)}/scene-answer`, {
      request_id: question.request_id,
      nonce: question.nonce,
      frames_sha256: question.frames_sha256,
      predicates,
    }),
};

/** The URL of an evidence frame; only a plain digest token becomes a URL. */
export function frameUrl(runId: string, sha256: string): string | null {
  return /^[0-9A-Za-z_-]{8,128}$/.test(sha256)
    ? `${BASE}/${run(runId)}/frames/${sha256}`
    : null;
}
