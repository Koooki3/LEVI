// Fixture journals of a subtask-loop run, in the fixed note contract
// (`subtask_*`, `vlm_*`, JSON detail), as `GET runs/{id}/subtasks` returns
// them.
import type { SubtaskNote, SubtasksResponse } from "../types";

export const PLAN_F = "f".repeat(64);
export const PLAN_R = "e".repeat(64);
export const FRAME = "c".repeat(64);

export function journal(
  notes: [string, Record<string, unknown> | null][],
  plans: SubtasksResponse["plans"] = {},
): SubtasksResponse {
  return {
    reset_mode: "subtask_loop",
    notes: notes.map(
      ([code, detail], i): SubtaskNote => ({
        seq: i + 10,
        at: 1_000_000 + i * 1000,
        code,
        detail,
      }),
    ),
    plans,
    truncated: false,
  };
}

export const forwardPlan = {
  ok: true,
  problem: null,
  subtasks: [
    {
      index: 0,
      pick: "red cup",
      place: "tray",
      done_when: "the red cup is on the tray",
      instruction: "put the red cup on the tray",
    },
    {
      index: 1,
      pick: "blue cup",
      place: "tray",
      done_when: "the blue cup is on the tray",
      instruction: "put the blue cup on the tray",
    },
  ],
};
export const reversePlan = {
  ok: true,
  problem: null,
  subtasks: [
    {
      index: 0,
      pick: "red cup",
      place: "table",
      done_when: "the red cup is on the table",
      instruction: "put the red cup back on the table",
    },
  ],
};

const start = [
  ["subtask_inventory", { items: ["red cup", "blue cup", "tray"], frames: [FRAME] }],
  ["subtask_inventory_confirmed", { items: ["red cup", "blue cup", "tray"], by: "operator" }],
  ["vlm_wake", { ok: true, mem_available_gib: 18.5, ms: 4200 }],
  ["subtask_plan", { round: 1, loop: "forward", task: "put both cups on the tray", n: 2, plan_file: "subtasks/r0-forward.json", sha256: PLAN_F }],
  ["vlm_sleep", { ok: true, mem_available_gib: 26.0, ms: 900 }],
] as [string, Record<string, unknown>][]; // prettier-ignore

const started = (loop: string, index: number, attempt: number, no: number) =>
  [
    "subtask_started",
    {
      round: 1,
      loop,
      index,
      attempt,
      instruction:
        loop === "forward"
          ? forwardPlan.subtasks[index].instruction
          : reversePlan.subtasks[index].instruction,
      episode_id: `run-1.${loop === "forward" ? "forward" : "reset"}.${String(no).padStart(4, "0")}`,
    },
  ] as [string, Record<string, unknown>];
const check = (
  loop: string,
  index: number,
  attempt: number,
  no: number,
  value: boolean | null,
) =>
  [
    "subtask_check",
    {
      round: 1,
      loop,
      index,
      attempt,
      episode_id: `run-1.${loop === "forward" ? "forward" : "reset"}.${String(no).padStart(4, "0")}`,
      value,
      frames: [
        { ref: "wrist:check-1", sha256: FRAME, stored: true },
        { ref: "front:check-2", sha256: "9".repeat(64), stored: false },
      ],
    },
  ] as [string, Record<string, unknown>];

/** Both forward subtasks done at the first attempt; the loop is over. */
export const happy = () =>
  journal(
    [
      ...start,
      started("forward", 0, 1, 1),
      check("forward", 0, 1, 1, true),
      started("forward", 1, 1, 2),
      check("forward", 1, 1, 2, true),
      [
        "subtask_loop_done",
        { round: 1, loop: "forward", subtasks: 2, retries: 0 },
      ],
    ],
    { [PLAN_F]: forwardPlan },
  );

/** Subtask 0 not done, resent, then done; subtask 1 running. */
export const retry = () =>
  journal(
    [
      ...start,
      started("forward", 0, 1, 1),
      check("forward", 0, 1, 1, false),
      ["subtask_retry", { round: 1, loop: "forward", index: 0, attempt: 2 }],
      started("forward", 0, 2, 2),
      check("forward", 0, 2, 2, true),
      started("forward", 1, 1, 3),
    ],
    { [PLAN_F]: forwardPlan },
  );

/** Subtask 0 not done, retry announced but not yet started. */
export const retryPending = () =>
  journal(
    [
      ...start,
      started("forward", 0, 1, 1),
      check("forward", 0, 1, 1, false),
      ["subtask_retry", { round: 1, loop: "forward", index: 0, attempt: 2 }],
    ],
    { [PLAN_F]: forwardPlan },
  );

/** The model could not tell: the run waits for a person. */
export const unknown = () =>
  journal(
    [...start, started("forward", 0, 1, 1), check("forward", 0, 1, 1, null)],
    { [PLAN_F]: forwardPlan },
  );

/** The model refused to plan the forward loop. */
export const refused = () =>
  journal([
    start[0],
    start[1],
    ["subtask_plan_refused", { round: 1, loop: "forward", reason: "object not in the whitelist: green cup" }],
  ]); // prettier-ignore

/** The VLM could not wake (not enough free memory). */
export const wakeFailed = () =>
  journal([
    start[0],
    start[1],
    ["vlm_sleep", { ok: true, mem_available_gib: 9.5, ms: 800 }],
    ["vlm_wake_failed", { reason: "MemAvailable 3.1 GiB below the 8 GiB floor" }],
  ]); // prettier-ignore

/** The forward loop finished, the reverse (restore) loop is running. */
export const reverse = () =>
  journal(
    [
      ...start,
      started("forward", 0, 1, 1),
      check("forward", 0, 1, 1, true),
      started("forward", 1, 1, 2),
      check("forward", 1, 1, 2, true),
      ["subtask_loop_done", { round: 1, loop: "forward", subtasks: 2, retries: 0 }],
      ["subtask_plan", { round: 1, loop: "reverse", task: "restore the scene", n: 1, plan_file: "subtasks/r0-reverse.json", sha256: PLAN_R }],
      started("reverse", 0, 1, 1),
    ],
    { [PLAN_F]: forwardPlan, [PLAN_R]: reversePlan },
  ); // prettier-ignore

/** The operator released round 1 at the terminal; the model was not woken. */
export const released = () =>
  journal([
    start[0],
    start[1],
    ["subtask_round_released", { round: 1, by: "terminal" }],
    ["vlm_wake_refused", { reason: "gpu_memory_short", free_mib: 900, need_mib: 20000 }],
  ]); // prettier-ignore

/** Round 2 given up after a stop. */
export const abandoned = () =>
  journal(
    [
      ...happy().notes.map((n) => [n.code, n.detail] as [string, Record<string, unknown> | null]),
      ["subtask_round_released", { round: 2, by: "terminal" }],
      ["subtask_round_abandoned", { round: 2, phase: "forward", why: "operator_stop" }],
    ],
    { [PLAN_F]: forwardPlan },
  ); // prettier-ignore

/** Terminal D's service steps, the alternation mode, then the inventory. */
export const services = () =>
  journal([
    ["subtask_service_step", { step: 1, key: "vlm_start", ok: true }],
    ["subtask_service_step", { step: 2, key: "vlm_ready", ok: false }],
    ["subtask_alternation", { mode: "co_resident", ok: false, headroom_mib: 900, need_mib: 6000 }],
    start[0],
  ]); // prettier-ignore
