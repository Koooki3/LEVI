import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import wire from "./reset-harness-wire.samples.json";
import {
  parseResetHarnessRun,
  parseResetHarnessCatalog,
  RESET_SCENARIOS,
  RESET_STAGES,
  RESET_STATUSES,
} from "../reset-harness-doc";
import {
  completedReset,
  queuedReset,
  resetCatalog,
} from "./reset-harness-fixtures";

describe("offline reset documents", () => {
  test("accepts actual offline worker and manager wire snapshots", () => {
    expect(parseResetHarnessRun(wire.queued)?.status).toBe("queued");
    expect(parseResetHarnessRun(wire.worker_lost)?.status).toBe("failed");
    const run = parseResetHarnessRun(wire.success);
    expect(run?.status).toBe("succeeded");
    expect(run?.final_observation?.evidence.released).toBe(true);
    expect(run?.final_observation?.evidence.object_at_target).toBe(true);
    expect(run?.target?.position_xyz).toEqual(
      run?.final_observation?.evidence.measured_object_xyz,
    );
  });
  test("accepts queued, recorded successful and unknown-holding blocked evidence", () => {
    expect(parseResetHarnessRun(queuedReset())).not.toBeNull();
    expect(parseResetHarnessRun(completedReset())).not.toBeNull();
    const blocked = queuedReset();
    Object.assign(blocked, {
      status: "blocked",
      stage: "lift_verify",
      stage_seq: 1,
    });
    blocked.events = [
      {
        seq: 1,
        stage: "lift_verify",
        status: "blocked",
        reason: "holding_measurement_missing",
        elapsed_ms: 1,
        evidence: {},
      },
    ];
    expect(parseResetHarnessRun(blocked)?.holding.status).toBe("unknown");
    expect(parseResetHarnessCatalog(resetCatalog([blocked]))).not.toBeNull();
  });
  test("rejects mixed physical modes and unknown schema/status/adapter", () => {
    for (const change of [
      { physical_execution: true },
      { execution_mode: "live" },
      { adapter: "robot" },
      { schema: "v2" },
      { status: "done" },
      { unexpected: true },
    ]) {
      expect(
        parseResetHarnessRun({ ...completedReset(), ...change }),
      ).toBeNull();
    }
    const run = completedReset();
    expect(
      parseResetHarnessRun({
        ...run,
        handoff: { ...run.handoff, physical_allowed: true },
      }),
    ).toBeNull();
    expect(
      parseResetHarnessCatalog({ ...resetCatalog(), physical_execution: true }),
    ).toBeNull();
  });
  test("keeps diagnostic stages as text, but refuses unknown event statuses", () => {
    const run = queuedReset();
    run.final_observation = null;
    run.status = "failed";
    run.stage = "worker_lost";
    run.stage_seq = 1;
    run.events = [
      {
        seq: 1,
        stage: "worker_lost",
        status: "failed",
        reason: "worker_lost",
        elapsed_ms: 1,
        evidence: {},
      },
    ];
    expect(parseResetHarnessRun(run)).not.toBeNull();
    expect(
      parseResetHarnessRun({
        ...run,
        events: [{ ...run.events[0], status: "maybe" }],
      }),
    ).toBeNull();
    expect(
      parseResetHarnessCatalog({
        ...resetCatalog([run]),
        issues: [{ run_id: "corrupt-run", code: "invalid_document" }],
      }),
    ).not.toBeNull();
  });
  test("never invents success or handoff from empty or inconsistent events", () => {
    const run = completedReset();
    for (const change of [
      { events: [], stage_seq: 0 },
      { stage_seq: 50 },
      { holding: { ...run.holding, status: "unknown" } },
      { final_observation: { ...run.final_observation, status: "unknown" } },
    ]) {
      expect(parseResetHarnessRun({ ...run, ...change })).toBeNull();
    }
    run.events[4].status = "running";
    expect(parseResetHarnessRun(run)).toBeNull();
    expect(
      parseResetHarnessRun({ ...queuedReset(), status: "succeeded" }),
    ).toBeNull();
    expect(
      parseResetHarnessRun({ ...completedReset(), status: "queued" }),
    ).toBeNull();
    expect(
      parseResetHarnessRun({ ...completedReset(), status: "blocked" }),
    ).toBeNull();
  });
  test("rejects malformed targets, counts, sequence and unmetered tokens reported as zero", () => {
    const run = completedReset();
    for (const change of [
      { seed: -1 },
      { seed: 0.5 },
      { elapsed_ms: NaN },
      { updated_at: "yesterday" },
      { target: { ...run.target, position_xyz: [1, 2, Infinity] } },
      { usage: { ...run.usage, input_tokens: 0 } },
    ]) {
      expect(parseResetHarnessRun({ ...run, ...change })).toBeNull();
    }
    run.events[0].seq = 2;
    expect(parseResetHarnessRun(run)).toBeNull();
  });
  test("rejects unknown/duplicate scenarios, non-contract labels and duplicate runs", () => {
    const catalog = resetCatalog();
    expect(
      parseResetHarnessCatalog({
        ...catalog,
        scenarios: [{ id: "live", label_key: "Live" }],
      }),
    ).toBeNull();
    expect(
      parseResetHarnessCatalog({
        ...catalog,
        scenarios: [catalog.scenarios[0], catalog.scenarios[0]],
      }),
    ).toBeNull();
    expect(
      parseResetHarnessCatalog({
        ...catalog,
        scenarios: [
          { ...catalog.scenarios[0], label_key: "marvel0.autonomy.start" },
        ],
      }),
    ).toBeNull();
    expect(
      parseResetHarnessCatalog(resetCatalog([queuedReset(), queuedReset()])),
    ).toBeNull();
  });
  test("every offline label has both languages with equal new key sets", () => {
    const E = en as Record<string, string>,
      Z = zh as Record<string, string>;
    const keys = Object.keys(E).filter((k) => k.startsWith("resetHarness."));
    expect(keys.sort()).toEqual(
      Object.keys(Z)
        .filter((k) => k.startsWith("resetHarness."))
        .sort(),
    );
    for (const [family, values] of [
      ["scenarios", RESET_SCENARIOS],
      ["stage", RESET_STAGES],
      ["status", RESET_STATUSES],
    ] as const) {
      for (const value of values) {
        expect(E[`resetHarness.${family}.${value}`]).toBeTruthy();
        expect(Z[`resetHarness.${family}.${value}`]).toBeTruthy();
      }
    }
  });
});
