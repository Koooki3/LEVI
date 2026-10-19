// The answers of GET /api/levi/rig/deploy, GET .../deploy/log and the two
// writes as the backend writes them (levi/rig/deploy.py: Supervisor._view,
// log, _launch, _stop_answer), with the real rig's numbers (a 32607 MiB card).
// A test replaces whole parts.
import { CHECKS } from "./backend-codes";

type Rec = Record<string, unknown>;

const OK_TEXT: Record<string, string> = {
  state_dir: "The folder for the deploy record is usable.",
  net_table: "The socket tables can be read.",
  port_free: "Nothing listens on the policy port.",
  no_client: "No client is connected to the policy port.",
  policy_units: "No policy server unit exists.",
  arm_lock: "The arm lock is free.",

  gpu_lock: "The GPU lock is free or held by the live vision model server.",
  gpu_readable: "The GPU's memory use can be read.",
  vram: "There is room for the smallest fraction.",
};

export function checks(over: Record<string, Rec> = {}): Rec[] {
  return CHECKS.map((code) => ({
    code,
    level: "ok",
    text: OK_TEXT[code],
    params: {},
    ...over[code],
  }));
}

export const UNIT = "levi-aeri-policy-manual-rig-pi05-fr3-all-step49999";

export function deployInfo(over: Rec = {}): Rec {
  return {
    request_id: "5b0c2d9e-7c1b-4f0e-9d3e-0a1b2c3d4e5f",
    unit: UNIT,
    model_id: "pi05_fr3_all_step49999",
    config: "pi05_fr3_all_state",
    rtc: true,
    fraction: 0.22,
    cfg_guidance_scale: null,
    phase: "started",
    started_at: "2026-10-19T03:12:09+08:00",
    age_s: 41.2,
    ready_at: null,
    gpu_lock: "taken",
    source_hash: {
      manifest: false,
      files: { "scripts/serve_policy_rtc.py": "ab" },
    },
    ...over,
  };
}

const presets = (free: number) =>
  [
    [0.22, 8198],
    [0.3, 10807],
    [0.45, 15698],
  ].map(([fraction, required]) => ({
    fraction,
    required_mib: required,
    enough: free >= required,
  }));

/** The idle answer: nothing runs, nothing is held, 23507 MiB are free. */
export function idle(over: Rec = {}): Rec {
  const free = 23507;
  return {
    schema: "levi.rig.deploy.v1",
    sampled_at: "2026-10-19T03:12:09+08:00",
    state: "idle",
    reason: null,
    deploy: null,
    unit: { state: "absent", sub: null },
    listener: { port: 8000, present: false, ours: null, established: 0 },
    readiness: {
      norm_stats: null,
      rtc_enabled: null,
      rtc_warmup: null,
      cfg: null,
      listening: null,
    },
    failure: null,
    gpu: {
      state: "ok",
      total_mib: 32607,
      used_mib: 32607 - free,
      free_mib: free,
      server_holds_mib: null,
      server_holds_basis: "reserved_fraction",
    },
    vram: {
      margin_mib: 1024,
      presets: presets(free),
      hint: "Fractions 0.30 and 0.45 need the live vLLM asleep or stopped",
    },
    checks: checks(),
    can_deploy: true,
    can_stop: false,
    stop_blockers: [],
    live_annotation: null,
    estop: {
      always: true,
      keep_hand: false,
      text: "Keep a hand on the physical e-stop.",
    },
    operation: null,
    notes: [
      {
        code: "source_hash_unmanifested",
        text: "No source-hash manifest is named in the site file.",
      },
    ],
    confirm: {
      deploy: "deploy {model_id}",
      stop: "stop policy server",
      client_override: "stop with client connected",
    },
    ...over,
  };
}

/** A server that is loading: the unit runs, nothing is logged yet. */
export function starting(over: Rec = {}): Rec {
  return idle({
    state: "starting",
    deploy: deployInfo(),
    unit: { state: "active", sub: "running" },
    readiness: {
      norm_stats: true,
      rtc_enabled: false,
      rtc_warmup: false,
      cfg: null,
      listening: false,
    },
    can_deploy: false,
    can_stop: true,
    gpu: {
      state: "ok",
      total_mib: 32607,
      used_mib: 9100,
      free_mib: 23507,
      server_holds_mib: 7174,
      server_holds_basis: "reserved_fraction",
    },
    live_annotation: {
      blocked: true,
      text: "server holds 7174 MiB, live annotation cannot start",
    },
    checks: checks({
      policy_units: {
        level: "refuse",
        text: "A policy server unit already exists",
        params: { units: [`${UNIT}.service`] },
      },
      port_free: { level: "ok" },
    }),
    ...over,
  });
}

/** A server that is ready: logged what it should, listens on the port. */
export function ready(over: Rec = {}): Rec {
  return starting({
    state: "ready",
    deploy: deployInfo({ ready_at: "2026-10-19T03:14:09+08:00" }),
    listener: { port: 8000, present: true, ours: true, established: 0 },
    readiness: {
      norm_stats: true,
      rtc_enabled: true,
      rtc_warmup: true,
      cfg: null,
      listening: true,
    },
    ...over,
  });
}

export function logAnswer(over: Rec = {}): Rec {
  return {
    schema: "levi.rig.deploy_log.v1",
    log_id: `${UNIT}-20261019T031200Z.log`,
    size_bytes: 18234,
    lines: [
      "INFO:root:Using norm stats shipped with the checkpoint: norm_stats.json",
      "INFO:root:RTC enabled: max_guidance_weight=5.0, schedule=exp, plain policy",
    ],
    classification: {
      norm_stats: true,
      rtc_enabled: true,
      rtc_warmup: false,
      cfg: null,
      listening: false,
      failure: null,
    },
    ...over,
  };
}

export function deployed(over: Rec = {}): Rec {
  return {
    schema: "levi.rig.deploy_result.v1",
    created: true,
    replayed: false,
    request_id: "5b0c2d9e-7c1b-4f0e-9d3e-0a1b2c3d4e5f",
    phase: "started",
    deploy: deployInfo(),
    state: "starting",
    gpu: {
      total_mib: 32607,
      free_mib_at_final_check: 23100,
      required_mib: 8198,
      lock: "taken",
    },
    next: "Watch the deploy state; the server is ready when the state is ready.",
    ...over,
  };
}

export function stopped(over: Rec = {}): Rec {
  return {
    schema: "levi.rig.stop_result.v1",
    created: true,
    replayed: false,
    request_id: "6c1d3eaf-8d2c-4a1f-8e4f-1b2c3d4e5f60",
    state: "idle",
    unit: UNIT,
    stopped: true,
    client_override_used: false,
    next: "Watch the deploy state; the port is free when the state is idle.",
    ...over,
  };
}
