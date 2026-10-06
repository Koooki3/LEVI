import { describe, expect, test } from "bun:test";
import {
  agreeOf,
  datasetFaultKind,
  detectFault,
  explainGate,
  filterReviewRuns,
  isEvaluating,
  isLost,
  needsPerson,
  PAUSE_NOTES,
  resetRemaining,
  nextDelay,
  rankDatasets,
  reviewRuns,
  rowSignature,
  segmentSummary,
  sortSessions,
  verdictReason,
  verdictTally,
} from "../live-logic";
import type {
  DatasetDetail,
  DatasetRow,
  DemoRow,
  LiveSession,
  ServiceStatus,
  Verdict,
} from "../types";

const session = (over: Partial<LiveSession> = {}): LiveSession => ({
  group: "pi05",
  task_folder: "stack_plates",
  state: "running",
  age_s: 0.5,
  ...over,
});
const row = (over: Partial<DatasetRow> = {}): DatasetRow => ({
  episodes: 4,
  pending: 0,
  annotating: 0,
  done: 4,
  failed: 0,
  ...over,
});

describe("polling", () => {
  test("2 s while an evaluation runs, 10 s otherwise", () => {
    expect(nextDelay(true, 0)).toBe(2000);
    expect(nextDelay(false, 0)).toBe(10000);
  });
  test("backs off after failures and is capped", () => {
    expect(nextDelay(true, 1)).toBe(4000);
    expect(nextDelay(true, 2)).toBe(8000);
    expect(nextDelay(false, 1)).toBe(20000);
    expect(nextDelay(false, 5)).toBe(30000);
    expect(nextDelay(true, 50)).toBe(30000);
  });
  test("an evaluation is going on when a session moves or the service works", () => {
    expect(isEvaluating([session({ state: "waiting_reset" })], null)).toBe(
      true,
    );
    expect(isEvaluating([session({ state: "finished" })], null)).toBe(false);
    expect(
      isEvaluating([session({ state: "running", crashed: true })], null),
    ).toBe(false);
    expect(isEvaluating([], { state: "annotating" } as ServiceStatus)).toBe(
      true,
    );
    expect(isEvaluating([], { state: "idle" } as ServiceStatus)).toBe(false);
  });
});

describe("lost contact", () => {
  test("a silent live session is lost; an ended one is not", () => {
    expect(isLost(session({ age_s: 12 }))).toBe(true);
    expect(isLost(session({ age_s: 3 }))).toBe(false);
    expect(isLost(session({ state: "finished", age_s: 9999 }))).toBe(false);
    expect(isLost(session({ state: "crashed", age_s: 9999 }))).toBe(false);
  });
});

describe("the FR3 banner", () => {
  test("a red light raises it, with the monitor's reasons", () => {
    const f = detectFault(
      { state: "red", reasons: ["robot_mode 4"], current_errors: ["reflex"] },
      [],
    );
    expect(f.active && f.redLight).toBe(true);
    expect(f.reasons).toEqual(["robot_mode 4"]);
    // With no reason given, the errors themselves are listed.
    expect(
      detectFault({ state: "red", current_errors: ["reflex"] }, []).reasons,
    ).toEqual(["reflex"]);
  });
  test("sessions sort faults first", () => {
    const order = sortSessions([
      session({ task_folder: "b", state: "finished" }),
      session({ task_folder: "c", state: "running" }),
      session({ task_folder: "a", state: "fault" }),
    ]).map((s) => s.task_folder);
    expect(order).toEqual(["a", "c", "b"]);
  });
  test("a session in fault raises it without a red light", () => {
    const f = detectFault({ state: "ok" }, [
      session({ state: "fault", reason: "joint reflex" }),
    ]);
    expect(f.active).toBe(true);
    expect(f.redLight).toBe(false);
    expect(f.reasons).toEqual(["joint reflex"]);
  });
  test("an offline or missing monitor is not a red light", () => {
    expect(detectFault({ state: "offline" }, []).active).toBe(false);
    expect(detectFault({ state: "missing" }, []).active).toBe(false);
    expect(detectFault(null, [session()]).active).toBe(false);
  });
  test("a dataset is marked now, or as an earlier fault, or not at all", () => {
    const faulted = [session({ state: "fault" })];
    const name = "pi05__stack_plates";
    expect(datasetFaultKind(name, row(), faulted, false)).toBe("current");
    expect(datasetFaultKind(name, row(), [session()], true)).toBe("current");
    expect(datasetFaultKind(name, row({ fr3_fault: 2 }), [], false)).toBe(
      "earlier",
    );
    expect(datasetFaultKind(name, row(), [], false)).toBeNull();
    // A red light does not mark a dataset whose evaluation is over.
    expect(
      datasetFaultKind(name, row(), [session({ state: "finished" })], true),
    ).toBeNull();
  });
});

describe("the GPU gate in words", () => {
  const service = (over: Partial<ServiceStatus>): ServiceStatus => ({
    state: "gpu_wait",
    gpu: { gate: { open: true }, decision: { allowed: true } },
    ...over,
  });
  test("while the policy infers, it says labelling waits and why", () => {
    const e = explainGate(
      service({ gpu: { gate: { open: false, code: "policy_inferring" } } }),
    );
    expect(e?.tone).toBe("warn");
    expect(e?.title).toContain("policy runs");
    expect(e?.detail).toContain("jitter");
  });
  test("an unknown policy server is explained", () => {
    const e = explainGate(
      service({ gpu: { gate: { open: false, code: "unknown_client" } } }),
    );
    expect(e?.title).toContain("cannot see");
  });
  test("an open gate waiting for memory names the reason", () => {
    const e = explainGate(
      service({
        gpu: {
          gate: { open: true },
          decision: { allowed: false, code: "vram" },
        },
      }),
    );
    expect(e?.detail).toContain("free GPU memory");
  });
  test("nothing without a service", () => {
    expect(explainGate(null)).toBeNull();
  });
});

const demos: DemoRow[] = [
  {
    demo: "demo_0003",
    state: "done",
    segments: 5,
    committed_at: 10,
    verdict: { run_id: "r2", outcome: "success", at: 300 },
  },
  {
    demo: "demo_0002",
    state: "done",
    segments: 4,
    committed_at: 9,
    verdict: { run_id: "r1", outcome: "failure", at: 200 },
  },
  {
    demo: "demo_0001",
    state: "done",
    segments: 3,
    committed_at: 8,
    verdict: { run_id: "r1", outcome: null, undecided: true, at: 190 },
  },
  { demo: "demo_0000", state: "done", segments: 0, verdict: null },
  { demo: "demo_0004", state: "mirrored" },
];
const detail = { enabled: true, name: "x", demos } as DatasetDetail;

describe("automatic results", () => {
  test("tally keeps success, failure, undecided and missing apart", () => {
    expect(verdictTally(demos)).toEqual({
      success: 1,
      failure: 1,
      undecided: 1,
      none: 1,
    });
    expect(verdictTally(undefined)).toEqual({
      success: 0,
      failure: 0,
      undecided: 0,
      none: 0,
    });
  });
  test("time segments count committed demos only", () => {
    expect(segmentSummary(demos)).toEqual({ committed: 3, segments: 12 });
  });
  test("review runs group by run id, newest first", () => {
    const runs = reviewRuns(detail);
    expect(runs.map((r) => r.runId)).toEqual(["r2", "r1"]);
    expect(runs[1]).toMatchObject({ demos: 2, failure: 1, undecided: 1 });
  });
  test("the view filter hides but never drops", () => {
    const many = Array.from({ length: 6 }, (_, i) => ({
      runId: `r${i}`,
      demos: 1,
      success: 1,
      failure: 0,
      undecided: 0,
      at: 1_000_000 - i * 50_000,
    }));
    const latest = filterReviewRuns(many, "latest", 1_000_000);
    expect(latest.shown).toHaveLength(3);
    expect(latest.hidden).toBe(3);
    const day = filterReviewRuns(many, "day", 1_000_000);
    expect(day.shown.length + day.hidden).toBe(6);
    expect(day.shown.every((r) => 1_000_000 - r.at <= 86400)).toBe(true);
    expect(filterReviewRuns(many, "all", 1_000_000).hidden).toBe(0);
  });
});

describe("dataset cards", () => {
  test("faulted and busy datasets come first", () => {
    const rows = {
      a__idle: row({ last_processed_at: 500 }),
      b__busy: row({ state: "annotating", last_processed_at: 10 }),
      c__fault: row({ last_processed_at: 1 }),
    };
    const order = rankDatasets(
      rows,
      [session({ group: "c", task_folder: "fault", state: "fault" })],
      false,
    );
    expect(order).toEqual(["c__fault", "b__busy", "a__idle"]);
  });
  test("the signature changes when a row says something new", () => {
    const a = rowSignature(row());
    expect(rowSignature(row())).toBe(a);
    expect(rowSignature(row({ done: 5 }))).not.toBe(a);
    expect(rowSignature(row({ last_error: "x" }))).not.toBe(a);
  });
});

describe("reset countdown", () => {
  const waiting = {
    state: "waiting_reset",
    reset_wait_s: 10,
    waiting_reset_since: 1000,
  };
  test("counts down from when the wait began and goes negative when over", () => {
    expect(resetRemaining(waiting, 1004)).toBe(6);
    expect(resetRemaining(waiting, 1010)).toBe(0);
    expect(resetRemaining(waiting, 1013)).toBe(-3);
  });
  test("nothing unless it is waiting and both numbers are known", () => {
    expect(resetRemaining({ ...waiting, state: "running" }, 1004)).toBeNull();
    expect(
      resetRemaining({ ...waiting, waiting_reset_since: null }, 1004),
    ).toBeNull();
    expect(resetRemaining({ ...waiting, reset_wait_s: null }, 1004)).toBeNull();
  });
});

describe("needs a person", () => {
  test("lists the attention, the waiting datasets and a failed page", () => {
    const need = needsPerson(
      {
        attention: { code: "vllm_failed", reason: "KV cache" },
        frontend: { state: "failed", error: "port busy", attempts: 2 },
      } as ServiceStatus,
      {
        a__x: row({ state: "awaiting_approval", awaiting: "plan" }),
        b__y: row({ state: "awaiting_approval", awaiting: "changes" }),
        c__z: row(),
      },
    );
    expect(need.attention?.reason).toBe("KV cache");
    expect(need.awaiting).toEqual([
      { name: "a__x", kind: "plan" },
      { name: "b__y", kind: "changes" },
    ]);
    expect(need.frontendFailed).toEqual({ error: "port busy", attempts: 2 });
  });
  test("nothing when all is well", () => {
    const need = needsPerson(
      { frontend: { state: "ok" } } as ServiceStatus,
      {},
    );
    expect(need.attention).toBeNull();
    expect(need.awaiting).toEqual([]);
    expect(need.frontendFailed).toBeNull();
  });
});

describe("labelling paused and a stuck loop", () => {
  const svc = {
    labelling_paused: { code: "insufficient_vram", reason: "9 GiB free" },
    attention: { code: "vllm_failed", reason: "x" },
    loop_at: 1000,
  } as ServiceStatus;
  test("a pause is shown with its reason and replaces the older attention", () => {
    const need = needsPerson(svc, {}, 1100);
    expect(need.paused?.code).toBe("insufficient_vram");
    expect(need.attention).toBeNull();
    expect(need.loopStalledS).toBeNull();
  });
  test("the loop is stuck after 300 s without a tick", () => {
    expect(needsPerson(svc, {}, 1300).loopStalledS).toBeNull();
    expect(needsPerson(svc, {}, 1400).loopStalledS).toBe(400);
  });
  test("every pause code has words and a thing to do", () => {
    for (const code of [
      "vllm_failed",
      "vllm_error",
      "insufficient_vram",
      "unknown_client",
      "policy_large",
      "vram",
      "lock",
      "external_busy",
    ]) {
      expect(PAUSE_NOTES[code].title.length).toBeGreaterThan(10);
      expect(PAUSE_NOTES[code].todo.length).toBeGreaterThan(10);
    }
    expect(PAUSE_NOTES.vllm_failed.command).toBe("levi live resume");
  });
  test("an evaluation under way explains why the model waits", () => {
    const e = explainGate({
      state: "gpu_wait",
      gpu: {
        gate: { open: true },
        decision: { allowed: false, code: "evaluation_active" },
      },
    });
    expect(e?.title).toContain("evaluation is under way");
  });
});

describe("the new gate and decision codes", () => {
  const svc = (
    gpu: ServiceStatus["gpu"],
    state = "gpu_wait",
  ): ServiceStatus => ({
    state,
    gpu,
  });
  test("an imminent episode closes the gate with its own words", () => {
    const e = explainGate(
      svc({ gate: { open: false, code: "episode_imminent" } }),
    );
    expect(e?.title).toContain("ahead of the next episode");
  });
  test.each([
    "insufficient_vram",
    "gate_closed",
    "backoff",
    "needs_attention",
    "policy_large",
    "prewarm_waiting_for_policy",
    "standby_settling",
    "gpu_not_free",
  ])("%s is explained", (code) => {
    const e = explainGate(
      svc({ gate: { open: true }, decision: { allowed: false, code } }),
    );
    expect(e?.tone).toBe("warn");
    expect(e?.detail.length).toBeGreaterThan(20);
    expect(e?.title).not.toBe("Waiting for the local model to start");
  });
  test("an asleep model server is a calm note", () => {
    const e = explainGate(
      svc(
        { gate: { open: true }, decision: { allowed: true, code: "asleep" } },
        "idle",
      ),
    );
    expect(e?.title).toContain("asleep");
    expect(e?.tone).toBe("");
  });
});

describe("open review runs", () => {
  test("only the runs the service still holds open are listed", () => {
    const runs = reviewRuns({
      ...detail,
      review_runs: ["r2"],
    } as DatasetDetail);
    expect(runs.map((r) => r.runId)).toEqual(["r2"]);
  });
});

describe("why a terminal-aware verdict is not a plain success", () => {
  const under = (over: Partial<Verdict>): Verdict => ({
    outcome: "failure",
    events: 2,
    valid_events: 1,
    rule: "last_valid_not_regrasped",
    ...over,
  });
  test("the default rule explains nothing, nor does a held success", () => {
    expect(verdictReason(null)).toBeNull();
    expect(verdictReason({ outcome: "failure", valid_events: 1 })).toBeNull();
    expect(verdictReason(under({ rule: "any_valid" }))).toBeNull();
    expect(
      verdictReason(
        under({
          outcome: "success",
          closes_after_last_valid: 0,
          place_outcome: "success",
        }),
      ),
    ).toBeNull();
  });
  test("too few valid releases keeps the ordinary explanation", () => {
    expect(
      verdictReason(
        under({ valid_events: 1, min_valid: 2, place_outcome: "failure" }),
      ),
    ).toBeNull();
    expect(
      verdictReason(
        under({ valid_events: 2, min_valid: 2, place_outcome: "failure" }),
      ),
    ).toBe("the last placement is a failure");
  });
  test("a record without close frames cannot be checked for a new grasp", () => {
    const why = (v: Partial<Verdict>) =>
      verdictReason(under({ closes_after_last_valid: null, ...v }));
    expect(why({ place_outcome: "success" })).toBe(
      "no gripper close frames recorded: a new grasp cannot be checked",
    );
    // A failed placement is the reason that matters, and 0 closes is a check done.
    expect(why({ place_outcome: "failure" })).toBe(
      "the last placement is a failure",
    );
    expect(
      why({ place_outcome: "success", closes_after_last_valid: 0 }),
    ).toBeNull();
  });
  test("a release with no valid event keeps the ordinary explanation", () => {
    expect(
      verdictReason(under({ valid_events: 0, place_outcome: "failure" })),
    ).toBeNull();
  });
  test("picked up again comes before the placement", () => {
    expect(
      verdictReason(
        under({ closes_after_last_valid: 2, place_outcome: "failure" }),
      ),
    ).toBe("the gripper closed again after the last release");
  });
  test("each placement outcome has its own wording", () => {
    const why = (place_outcome: string) =>
      verdictReason(under({ closes_after_last_valid: 0, place_outcome }));
    expect(why("failure")).toBe("the last placement is a failure");
    expect(why("none")).toBe("no placement time segment");
    expect(why("unknown")).toBe("the last placement is undecided");
    expect(why("missing")).toBe("placement not checked: no time segments");
  });
});

describe("agent vs operator", () => {
  const v = (outcome: string | null, undecided = false): Verdict => ({
    outcome,
    undecided,
  });
  test("one episode agrees, disagrees, is undecided or has no verdict yet", () => {
    expect(agreeOf({ outcome: "success" }, v("success"))).toBe("yes");
    expect(agreeOf("failure", v("failure"))).toBe("yes");
    expect(agreeOf({ outcome: "failure", by: "operator" }, v("success"))).toBe(
      "no",
    );
    expect(agreeOf("success", v("success", true))).toBe("undecided");
    expect(agreeOf("success", v(null, true))).toBe("undecided");
    expect(agreeOf("failure", null)).toBe("no_agent");
    expect(agreeOf("failure", v(null))).toBe("no_agent");
  });
  test("no operator success or failure is no pair at all", () => {
    for (const op of [
      null,
      undefined,
      "unlabeled",
      "discarded",
      { outcome: null },
    ])
      expect(agreeOf(op as string | null, v("success"))).toBeNull();
  });
  test("the automatic tally ignores the operator label", () => {
    const labelled = demos.map((d) => ({
      ...d,
      operator_label: { outcome: "failure", by: "operator" },
      agreement: "no" as const,
    }));
    expect(verdictTally(labelled)).toEqual(verdictTally(demos));
  });
});
