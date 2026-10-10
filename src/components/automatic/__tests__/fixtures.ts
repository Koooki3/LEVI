// Shared fixtures for the automatic-run tests (shapes from the API contract).
import type {
  JobsResponse,
  LaunchPlan,
  PendingCard,
  RunSnapshot,
  SceneQuestion,
} from "../types";

export const SHA = "a".repeat(64);

export function card(over: Partial<PendingCard> = {}): PendingCard {
  return {
    reason: "scene_reset_required",
    resume_seq: 41,
    waited_ms: 65_000,
    nth_wait: 2,
    last_episode: {
      episode_id: "ep-0007",
      rollout_label: "rollout says early stop",
      ended_by: "early_stop",
      stop_reason: "goal_verified",
    },
    contract: {
      id_version: "plates@3",
      status: "draft",
      predicates: ["the plate is on the rack", "the cup is upright"],
    },
    assessment: {
      decision: "reset_required",
      failed: ["the cup is upright"],
      unknown: [],
      frame_refs: [SHA],
    },
    operator_label: {
      current: null,
      automatic_verdict: "success",
      hidden_until_labelled: true,
    },
    ...over,
  };
}

export function snapshot(over: Partial<RunSnapshot> = {}): RunSnapshot {
  return {
    run_id: "run-1",
    state: "WAIT_HUMAN",
    seq: 41,
    reset_mode: "human_assisted",
    scene_check: "provider",
    execution_mode: "dry_run",
    episodes: {
      done: 6,
      total: 20,
      current: { no: 7, step: 12, max_steps: 300 },
    },
    counters: {
      planned_interventions: 3,
      unplanned_interventions: 1,
      faults: 0,
    },
    pending_card: null,
    scene_question: null,
    challenge: "chal-1",
    runner: { alive: true, pid: 4242 },
    ...over,
  };
}

export function question(over: Partial<SceneQuestion> = {}): SceneQuestion {
  return {
    request_id: "q-1",
    nonce: "n-1",
    frames_sha256: "f".repeat(64),
    frames: [SHA],
    predicates: [
      {
        name: "plate_on_rack",
        text: "the plate is on the rack",
        required: true,
      },
      { name: "cup_upright", text: "the cup is upright", required: true },
      { name: "lid_off", text: "the lid is off", required: false },
    ],
    asked_at: Date.now(),
    timeout_s: 600,
    ...over,
  };
}

export function plan(over: Partial<LaunchPlan> = {}): LaunchPlan {
  return {
    plan_sha256: "p".repeat(64),
    run_id: "run-new",
    execution_mode: "dry_run",
    reset_mode: "human_assisted",
    scene_check: "operator_attested",
    roles: ["forward"],
    episodes: 20,
    launchable: true,
    refusals: [],
    checks: [{ code: "job_valid", ok: true, severity: "info", detail: "ok" }],
    isc: { id: "plates", version: 3, status: "draft" },
    uses_live: { c5: false, gate: false },
    motion: false,
    launch_token: "tok",
    token_expires_at: Date.now() + 60_000,
    ...over,
  };
}

/** A fetch mock that answers by method + path; records every call. */
export type Call = { method: string; path: string; body: unknown };
export function mockFetch(
  routes: Record<string, (body: unknown) => unknown | Promise<unknown>>,
): { calls: Call[]; restore: () => void } {
  const original = globalThis.fetch;
  const calls: Call[] = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const url = String(input).replace("/api/levi/automatic/", "");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, path: url, body });
    const route = routes[`${method} ${url}`] ?? routes[`${method} *`];
    if (!route) return new Response("{}", { status: 404 });
    const out = (await route(body)) as
      | { __status: number; body: unknown }
      | unknown;
    if (out && typeof out === "object" && "__status" in out) {
      const { __status, body: payload } = out as {
        __status: number;
        body: unknown;
      };
      return new Response(JSON.stringify(payload), { status: __status });
    }
    return new Response(JSON.stringify(out), { status: 200 });
  }) as typeof fetch;
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

export function fixtureJobs(): JobsResponse {
  return {
    roots: ["jobs"],
    jobs: [
      {
        id: "job-1",
        name: "plates-eval",
        valid: true,
        reset_mode: "human_assisted",
      },
      {
        id: "job-2",
        name: "broken",
        valid: false,
        errors: ["task.instruction: required"],
      },
    ],
  };
}
