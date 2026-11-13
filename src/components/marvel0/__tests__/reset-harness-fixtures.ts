import type { ResetHarnessApi } from "../reset-harness-api";
import {
  RESET_SCENARIOS,
  type ResetHarnessRun,
  type ResetHarnessCatalog,
} from "../reset-harness-doc";

export function queuedReset(id = "offline-run-1"): ResetHarnessRun {
  return {
    schema: "levi.reset_harness.run.v1",
    execution_mode: "offline",
    adapter: "fake",
    physical_execution: false,
    run_id: id,
    scenario_id: "normal_random",
    seed: 0,
    status: "queued",
    stage: "created",
    stage_seq: 0,
    events: [],
    target: null,
    holding: { status: "unknown", reason: "not_observed", evidence: {} },
    final_observation: {
      status: "unknown",
      reason: "not_observed",
      evidence: {},
    },
    handoff: {
      eligible: false,
      physical_allowed: false,
      reason: "not_checked",
      policy_epoch: null,
    },
    usage: {
      llm_calls: 0,
      input_tokens: null,
      output_tokens: null,
      token_source: "unavailable",
      provider: "scripted",
    },
    elapsed_ms: 0,
    updated_at: "2026-11-13T12:00:00Z",
  };
}
export function completedReset(id = "offline-complete"): ResetHarnessRun {
  const run = queuedReset(id);
  run.status = "succeeded";
  run.stage = "complete";
  run.events = [
    "observe",
    "plan",
    "regrasp",
    "lift_verify",
    "handoff",
    "policy",
    "release",
    "final_verify",
    "complete",
  ].map((stage, index) => ({
    seq: index + 1,
    stage: stage as ResetHarnessRun["stage"],
    status: "succeeded",
    reason: "synthetic_check_passed",
    elapsed_ms: (index + 1) * 10,
    evidence: { source: "synthetic_measurement" },
  }));
  run.stage_seq = run.events.length;
  run.elapsed_ms = 90;
  run.target = {
    object_id: "remote",
    region_id: "table_left",
    position_xyz: [0.42, -0.21, 0.1],
    frame_id: "fake_base",
    source: "seeded_synthetic",
  };
  run.holding = {
    status: "pass",
    reason: "post_lift_follow_and_gripper_measured",
    evidence: { object_followed: true },
  };
  run.final_observation = {
    status: "pass",
    reason: "synthetic_final_reset_verified",
    evidence: { released: true, object_at_target: true },
  };
  run.handoff = {
    eligible: true,
    physical_allowed: false,
    reason: "offline_handoff_verified",
    policy_epoch: 2,
  };
  return run;
}
export function resetCatalog(
  runs: ResetHarnessRun[] = [],
): ResetHarnessCatalog {
  return {
    schema: "levi.reset_harness.catalog.v1",
    execution_mode: "offline",
    physical_execution: false,
    scenarios: RESET_SCENARIOS.map((id) => ({
      id,
      label_key: `resetHarness.scenarios.${id}`,
    })),
    runs,
  };
}
export function fakeResetApi(initial: ResetHarnessRun[] = []) {
  const state = {
    runs: initial,
    reads: [] as string[],
    writes: [] as { kind: string; body: unknown; id?: string }[],
  };
  const ok = (data: unknown) =>
    Promise.resolve({
      ok: true as const,
      status: 200,
      data: structuredClone(data),
    });
  const api: ResetHarnessApi = {
    catalog: () => {
      state.reads.push("catalog");
      return ok(resetCatalog(state.runs));
    },
    run: (id) => {
      state.reads.push(id);
      return ok(state.runs.find((r) => r.run_id === id) ?? null);
    },
    start: (body) => {
      state.writes.push({ kind: "start", body });
      const run = queuedReset(`offline-run-${state.runs.length + 1}`);
      run.seed = body.seed;
      run.scenario_id = body.scenario_id;
      state.runs.unshift(run);
      return ok(run);
    },
    stop: (id, body) => {
      state.writes.push({ kind: "stop", body, id });
      const run = state.runs.find((r) => r.run_id === id)!;
      run.events.push({
        seq: run.events.length + 1,
        stage: "complete",
        status: "stopped",
        reason: "operator_stop",
        elapsed_ms: 20,
        evidence: {},
      });
      run.stage = "complete";
      run.stage_seq = run.events.length;
      run.status = "stopped";
      return ok(run);
    },
  };
  return { api, state };
}
