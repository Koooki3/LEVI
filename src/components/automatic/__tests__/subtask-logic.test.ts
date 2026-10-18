import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { eventReason } from "../run-logic";
import { foldSubtasks, hiddenEpisodesOf } from "../subtask-logic";
import {
  FRAME,
  PLAN_F,
  happy,
  abandoned,
  journal,
  released,
  refused,
  retry,
  retryPending,
  services,
  reverse,
  unknown,
  wakeFailed,
} from "./subtask-fixtures";

const statuses = (view: ReturnType<typeof foldSubtasks>, r = 0, l = 0) =>
  view.rounds[r].loops[l].subtasks.map((s) => s.status);

describe("folding a subtask-loop journal", () => {
  test("happy path: plan, two subtasks done, loop over", () => {
    const view = foldSubtasks(happy());
    expect(view.inventory).toEqual({
      status: "confirmed",
      items: ["red cup", "blue cup", "tray"],
      frames: [{ ref: null, sha256: FRAME, stored: true }],
      by: "operator",
    });
    const loop = view.rounds[0].loops[0];
    expect(loop.loop).toBe("forward");
    expect(loop.planOk).toBe(true);
    expect(loop.task).toBe("put both cups on the tray");
    expect(loop.done).toEqual({ subtasks: 2, retries: 0 });
    expect(statuses(view)).toEqual(["done", "done"]);
    expect(loop.subtasks[0].instruction).toBe("put the red cup on the tray");
    expect(loop.subtasks[0].attempts[0].frames).toEqual([
      { ref: "wrist:check-1", sha256: FRAME, stored: true },
      { ref: "front:check-2", sha256: "9".repeat(64), stored: false },
    ]);
    // A plain digest (the inventory here) counts as stored.
    expect(view.inventory?.frames).toEqual([
      { ref: null, sha256: FRAME, stored: true },
    ]);
    expect(view.current).toEqual({ round: 1, loop: "forward", index: 1 });
  });

  test("retry: not done, resent, done; the next one runs", () => {
    const view = foldSubtasks(retry());
    expect(statuses(view)).toEqual(["done", "running"]);
    const first = view.rounds[0].loops[0].subtasks[0];
    expect(first.attempts.map((a) => a.check)).toEqual([false, true]);
  });

  test("a retry announced but not yet started is 'retrying'", () => {
    expect(statuses(foldSubtasks(retryPending()))).toEqual([
      "retrying",
      "pending",
    ]);
  });

  test("not done past the retry limit is 'failed'", () => {
    const view = foldSubtasks(retryPending(), { maxRetries: 0 });
    expect(statuses(view)[0]).toBe("failed");
  });

  test("the model could not tell: 'unknown', never done", () => {
    expect(statuses(foldSubtasks(unknown()))[0]).toBe("unknown");
  });

  test("a refused plan has its reason and no subtasks", () => {
    const loop = foldSubtasks(refused()).rounds[0].loops[0];
    expect(loop.refused).toContain("green cup");
    expect(loop.subtasks).toEqual([]);
  });

  test("a failed wake keeps the last MemAvailable", () => {
    const view = foldSubtasks(wakeFailed());
    expect(view.vlm?.state).toBe("wake_failed");
    expect(view.vlm?.reason).toContain("below");
    expect(view.vlm?.memAvailableGib).toBe(9.5);
  });

  test("reverse loop: subtasks are reset episodes; forward loop is closed", () => {
    const view = foldSubtasks(reverse());
    expect(view.rounds[0].loops.map((l) => l.loop)).toEqual([
      "forward",
      "reverse",
    ]);
    const back = view.rounds[0].loops[1];
    expect(back.subtasks[0].status).toBe("running");
    expect(back.subtasks[0].attempts[0].episodeId).toBe("run-1.reset.0001");
    expect(view.current).toEqual({ round: 1, loop: "reverse", index: 0 });
    expect(view.vlm?.state).toBe("asleep");
  });

  test("a check of an episode awaiting its label is withheld", () => {
    const hidden = hiddenEpisodesOf({
      last_episode: { episode_id: "run-1.forward.0001" },
      operator_label: { hidden_until_labelled: true },
    });
    const view = foldSubtasks(unknown(), { hiddenEpisodes: hidden });
    expect(statuses(view)[0]).toBe("hidden");
    expect(view.rounds[0].loops[0].subtasks[0].attempts[0].check).toBe(
      "hidden",
    );
  });

  test("a plan file that did not match its digest falls back to the notes", () => {
    const data = retry();
    data.plans[PLAN_F] = {
      ok: false,
      problem: "sha256_mismatch",
      subtasks: [],
    };
    const loop = foldSubtasks(data).rounds[0].loops[0];
    expect(loop.planOk).toBe(false);
    expect(loop.planProblem).toBe("sha256_mismatch");
    expect(loop.subtasks.map((s) => s.instruction)).toEqual([
      "put the red cup on the tray",
      "put the blue cup on the tray",
    ]);
  });

  test("a round released at the terminal, the model not woken", () => {
    const view = foldSubtasks(released());
    expect(view.rounds).toEqual([
      { round: 1, released: "terminal", abandoned: null, loops: [] },
    ]);
    expect(view.vlm?.state).toBe("wake_refused");
    expect(view.vlm?.reason).toBe("gpu_memory_short");
  });

  test("a round given up keeps its reason; earlier rounds stay", () => {
    const view = foldSubtasks(abandoned());
    expect(view.rounds.map((r) => [r.round, r.abandoned])).toEqual([
      [1, null],
      [2, "operator_stop"],
    ]);
  });

  test("a sleep note that keeps the model loaded is 'idle', not asleep", () => {
    const view = foldSubtasks(
      journal([
        [
          "vlm_sleep",
          { ok: true, level: "stay_awake", mem_available_gib: 20, ms: 0 },
        ],
      ]),
    );
    expect(view.vlm?.state).toBe("idle");
  });

  test("service steps and the alternation mode are kept in order", () => {
    const view = foldSubtasks(services());
    expect(view.serviceSteps).toEqual([
      { step: 1, key: "vlm_start", ok: true },
      { step: 2, key: "vlm_ready", ok: false },
    ]);
    expect(view.alternation).toEqual({ mode: "co_resident", ok: false });
  });

  test("a cut detail is counted, never guessed", () => {
    const data = journal([]);
    data.notes.push({
      seq: 1,
      at: 0,
      code: "subtask_started",
      detail: null,
      unparsed: true,
    });
    expect(foldSubtasks(data).unreadable).toBe(1);
    expect(foldSubtasks(null).rounds).toEqual([]);
  });
});

// The note codes of the subtask-loop contract, as the timeline names them.
const NOTE_CODES = [
  "subtask_inventory",
  "subtask_inventory_confirmed",
  "subtask_inventory_rejected",
  "subtask_plan",
  "subtask_plan_refused",
  "subtask_started",
  "subtask_check",
  "subtask_retry",
  "subtask_loop_done",
  "vlm_sleep",
  "vlm_wake",
  "vlm_wake_failed",
  "vlm_sleep_refused",
  "subtask_round_released",
  "subtask_round_abandoned",
  "vlm_wake_refused",
  "subtask_alternation",
  "subtask_service_step",
  // Transition reasons of the mode (subtask_loop.py R_*).
  "subtask_dispatched",
  "subtask_continue",
  "subtask_ended",
  "subtask_check_unavailable",
  "subtask_retry_limit",
  "subtask_not_released",
  "vlm_unavailable",
];
describe("every subtask-loop note has words in both languages", () => {
  test.each(NOTE_CODES)("%s", (code) => {
    const english = eventReason(
      code,
      (k) => (en as Record<string, string>)[k] ?? k,
    );
    const chinese = eventReason(
      code,
      (k) => (zh as Record<string, string>)[k] ?? k,
    );
    expect(english).not.toBe(code);
    expect(chinese).toMatch(/[一-鿿]/);
  });
});
