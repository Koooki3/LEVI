// A closed offline-only document contract. A live or unknown adapter is never
// rendered as an offline rehearsal, and progress comes only from saved events.
export const RESET_SCENARIOS = [
  "normal_random",
  "empty_grasp_retry",
  "held_unknown",
  "stale_obs",
  "handoff_mismatch",
  "unreachable",
  "stop",
] as const;
export const RESET_STAGES = [
  "created",
  "observe",
  "plan",
  "open_gripper",
  "approach",
  "reset",
  "regrasp",
  "lift_verify",
  "handoff",
  "policy",
  "release",
  "final_verify",
  "complete",
] as const;
export const RESET_STATUSES = [
  "queued",
  "running",
  "succeeded",
  "blocked",
  "failed",
  "stopped",
] as const;
export type ResetScenario = (typeof RESET_SCENARIOS)[number];
export type ResetStage = (typeof RESET_STAGES)[number];
export type ResetStatus = (typeof RESET_STATUSES)[number];
export interface ResetEvent {
  seq: number;
  stage: string;
  status: "running" | "succeeded" | "blocked" | "failed" | "stopped";
  reason: string;
  elapsed_ms: number;
  evidence: Record<string, unknown>;
}
export interface ResetHarnessRun {
  schema: "levi.reset_harness.run.v1";
  execution_mode: "offline";
  adapter: "fake";
  physical_execution: false;
  run_id: string;
  scenario_id: ResetScenario;
  seed: number;
  status: ResetStatus;
  stage: string;
  stage_seq: number;
  events: ResetEvent[];
  target: {
    object_id: string;
    region_id: string;
    position_xyz: [number, number, number];
    frame_id: string;
    source: string;
  } | null;
  holding: {
    status: "pass" | "fail" | "unknown";
    reason: string;
    evidence: Record<string, unknown>;
  };
  final_observation: {
    status: "pass" | "fail" | "unknown";
    reason: string;
    evidence: Record<string, unknown>;
  } | null;
  handoff: {
    eligible: boolean;
    physical_allowed: false;
    reason: string;
    policy_epoch: number | null;
  };
  usage: {
    llm_calls: 0;
    input_tokens: null;
    output_tokens: null;
    token_source: "unavailable";
    provider: "scripted";
  };
  elapsed_ms: number;
  updated_at: string;
}
export interface ResetHarnessCatalog {
  schema: "levi.reset_harness.catalog.v1";
  execution_mode: "offline";
  physical_execution: false;
  scenarios: { id: ResetScenario; label_key: string }[];
  runs: ResetHarnessRun[];
  issues?: { run_id: string; code: string }[];
}

const record = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v);
const text = (v: unknown): v is string =>
  typeof v === "string" && v.length > 0 && v.length <= 4096;
const number = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);
const count = (v: unknown): v is number =>
  number(v) && Number.isSafeInteger(v) && v >= 0;
const oneOf = <T extends string>(v: unknown, values: readonly T[]): v is T =>
  typeof v === "string" && values.includes(v as T);
const exact = (v: Record<string, unknown>, keys: string): boolean => {
  const allowed = keys.split(" ");
  return (
    Object.keys(v).length === allowed.length && allowed.every((k) => k in v)
  );
};
const safeId = (v: unknown): v is string =>
  typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(v);
const json = (v: unknown, depth = 0): boolean => {
  if (depth > 20) return false;
  if (v === null || typeof v === "string" || typeof v === "boolean")
    return true;
  if (typeof v === "number") return Number.isFinite(v);
  if (Array.isArray(v)) return v.every((x) => json(x, depth + 1));
  return record(v) && Object.values(v).every((x) => json(x, depth + 1));
};
const evidence = (v: unknown): boolean => record(v) && json(v);

export function parseResetHarnessRun(data: unknown): ResetHarnessRun | null {
  if (
    !record(data) ||
    !exact(
      data,
      "schema execution_mode adapter physical_execution run_id scenario_id seed status stage stage_seq events target holding final_observation handoff usage elapsed_ms updated_at",
    )
  )
    return null;
  if (
    data.schema !== "levi.reset_harness.run.v1" ||
    data.execution_mode !== "offline" ||
    data.adapter !== "fake" ||
    data.physical_execution !== false
  )
    return null;
  if (
    !safeId(data.run_id) ||
    !oneOf(data.scenario_id, RESET_SCENARIOS) ||
    !count(data.seed) ||
    data.seed > 0xffffffff ||
    !oneOf(data.status, RESET_STATUSES) ||
    !text(data.stage) ||
    !count(data.stage_seq) ||
    !number(data.elapsed_ms) ||
    data.elapsed_ms < 0
  )
    return null;
  if (
    !text(data.updated_at) ||
    !/T.*(?:Z|\+00:00)$/.test(data.updated_at) ||
    !Number.isFinite(Date.parse(data.updated_at))
  )
    return null;
  if (!Array.isArray(data.events) || data.events.length > 10000) return null;
  for (const [index, event] of data.events.entries()) {
    if (
      !record(event) ||
      !exact(event, "seq stage status reason elapsed_ms evidence") ||
      event.seq !== index + 1 ||
      !text(event.stage) ||
      !oneOf(event.status, [
        "running",
        "succeeded",
        "blocked",
        "failed",
        "stopped",
      ]) ||
      !text(event.reason) ||
      !number(event.elapsed_ms) ||
      event.elapsed_ms < 0 ||
      !evidence(event.evidence)
    )
      return null;
  }
  const events = data.events as ResetEvent[];
  const last = events.at(-1);
  if (
    data.stage_seq !== events.length ||
    (last ? data.stage !== last.stage : data.stage !== "created")
  )
    return null;
  if (!last && data.status !== "queued" && data.status !== "running")
    return null;
  if (data.status === "queued" && last) return null;
  if (
    oneOf(data.status, ["blocked", "failed", "stopped"]) &&
    last?.status !== data.status
  )
    return null;
  const holding = data.holding,
    handoff = data.handoff,
    usage = data.usage;
  if (
    !record(holding) ||
    !exact(holding, "status reason evidence") ||
    !oneOf(holding.status, ["pass", "fail", "unknown"]) ||
    !text(holding.reason) ||
    !evidence(holding.evidence)
  )
    return null;
  if (
    !record(handoff) ||
    !exact(handoff, "eligible physical_allowed reason policy_epoch") ||
    typeof handoff.eligible !== "boolean" ||
    handoff.physical_allowed !== false ||
    !text(handoff.reason) ||
    !(handoff.policy_epoch === null || count(handoff.policy_epoch))
  )
    return null;
  if (
    handoff.eligible &&
    (holding.status !== "pass" ||
      handoff.policy_epoch === null ||
      !events.some((e) => e.stage === "handoff" && e.status === "succeeded"))
  )
    return null;
  if (
    holding.status === "pass" &&
    !events.some((e) => e.stage === "lift_verify")
  )
    return null;
  const final = data.final_observation;
  if (
    final !== null &&
    (!record(final) ||
      !exact(final, "status reason evidence") ||
      !oneOf(final.status, ["pass", "fail", "unknown"]) ||
      !text(final.reason) ||
      !evidence(final.evidence))
  )
    return null;
  if (
    record(final) &&
    final.status === "pass" &&
    !events.some((e) => e.stage === "final_verify" && e.status === "succeeded")
  )
    return null;
  if (
    data.status === "succeeded" &&
    (last?.stage !== "complete" ||
      last.status !== "succeeded" ||
      !handoff.eligible ||
      !record(final) ||
      final.status !== "pass")
  )
    return null;
  if (
    !record(usage) ||
    !exact(
      usage,
      "llm_calls input_tokens output_tokens token_source provider",
    ) ||
    usage.llm_calls !== 0 ||
    usage.input_tokens !== null ||
    usage.output_tokens !== null ||
    usage.token_source !== "unavailable" ||
    usage.provider !== "scripted"
  )
    return null;
  const target = data.target;
  if (
    target !== null &&
    (!record(target) ||
      !exact(target, "object_id region_id position_xyz frame_id source") ||
      !text(target.object_id) ||
      !text(target.region_id) ||
      !text(target.frame_id) ||
      !text(target.source) ||
      !Array.isArray(target.position_xyz) ||
      target.position_xyz.length !== 3 ||
      !target.position_xyz.every(number))
  )
    return null;
  return data as unknown as ResetHarnessRun;
}

export function parseResetHarnessCatalog(
  data: unknown,
): ResetHarnessCatalog | null {
  if (
    !record(data) ||
    !exact(
      data,
      "schema execution_mode physical_execution scenarios runs" +
        ("issues" in data ? " issues" : ""),
    ) ||
    data.schema !== "levi.reset_harness.catalog.v1" ||
    data.execution_mode !== "offline" ||
    data.physical_execution !== false ||
    !Array.isArray(data.scenarios) ||
    !Array.isArray(data.runs)
  )
    return null;
  if (
    "issues" in data &&
    (!Array.isArray(data.issues) ||
      !data.issues.every(
        (issue) =>
          record(issue) &&
          exact(issue, "run_id code") &&
          safeId(issue.run_id) &&
          text(issue.code),
      ))
  )
    return null;
  const ids = new Set<string>();
  for (const scenario of data.scenarios) {
    if (
      !record(scenario) ||
      !exact(scenario, "id label_key") ||
      !oneOf(scenario.id, RESET_SCENARIOS) ||
      scenario.label_key !== `resetHarness.scenarios.${scenario.id}` ||
      ids.has(scenario.id)
    )
      return null;
    ids.add(scenario.id);
  }
  const runs = data.runs.map(parseResetHarnessRun);
  if (
    runs.some((run) => run === null) ||
    new Set(runs.map((run) => run!.run_id)).size !== runs.length
  )
    return null;
  return data as unknown as ResetHarnessCatalog;
}

export const resetRunActive = (run: ResetHarnessRun): boolean =>
  run.status === "queued" || run.status === "running";
