// Answers of the two status routes as the servers write them, with one
// running marvel0 as the default. A test replaces whole parts. The setup status
// is built by hand after levi/setup/probes.py and docs/SETUP_WIZARD.md; the
// marvel0 status is the backend's own samples (marvel0-status.sample.json, busy, and
// marvel0-status.quiet.sample.json, idle, rewritten by tests/marvel0/regen_sample.py),
// so a change of their shape reaches these tests. A variant is made by
// changing a copy of one of the two.
import sample from "./marvel0-status.sample.json";
import quiet from "./marvel0-status.quiet.sample.json";

type Rec = Record<string, unknown>;

export const PORT_ROLES: Array<[number, string]> = [
  [5000, "robot_server"],
  [5001, "robot_server_2"],
  [5100, "robot_tunnel"],
  [7470, "learner"],
  [7860, "product_ui"],
  [7861, "product_core"],
  [7880, "live_viewer"],
  [7881, "live_core"],
  [7882, "online_judge"],
  [8000, "policy_server"],
  [8100, "vllm"],
];

/** The ports part: `listening` names the ports with a LISTEN socket;
 * `omit` leaves ports out of the table (a port the status does not watch). */
export function portsPart(
  listening: number[],
  omit: number[] = [],
  process = "python",
): Rec {
  return {
    state: "ok",
    truncated: false,
    ports: PORT_ROLES.filter(([port]) => !omit.includes(port)).map(
      ([port, role]) => ({
        port,
        role,
        listening: listening.includes(port),
        scope: listening.includes(port) ? ["loopback"] : [],
        pid: listening.includes(port) ? 4000 + port : null,
        process: listening.includes(port) ? process : null,
        owner_known: true,
      }),
    ),
  };
}

export function healthPart(over: Rec = {}): Rec {
  return {
    state: "ok",
    age_s: 0.4,
    robot_mode: 1,
    robot_mode_name: "Idle",
    current_errors: [],
    last_motion_errors: [],
    hardware_active: true,
    controller_active: true,
    command_success_rate: 1,
    reasons: [],
    ...over,
  };
}

export function gpuPart(over: Rec = {}): Rec {
  return {
    state: "ok",
    gpus: [
      {
        index: 0,
        name: "NVIDIA GeForce RTX 5090",
        total_mib: 32768,
        used_mib: 12288,
        free_mib: 20480,
        util_pct: 7,
      },
    ],
    processes: [
      { pid: 4008, name: "python", memory_mib: 9216, role: "policy_server" },
    ],
    ...over,
  };
}

export function lockPart(): Rec {
  return {
    state: "ok",
    locks: [
      {
        label: "levi",
        state: "held",
        holders: [
          { pid: 4008, process: "python", kind: "FLOCK", mode: "WRITE" },
        ],
      },
      { label: "live", state: "free", holders: [] },
    ],
  };
}

export function livePart(over: Rec = {}): Rec {
  return {
    state: "ok",
    alive: true,
    service_state: "idle",
    age_s: 1.2,
    vllm_state: "awake",
    gpu_mode: "auto",
    gate: { open: true, code: "open", reason: "" },
    decision: { allowed: true, code: "ok" },
    labelling_paused: false,
    ...over,
  };
}

/** A running marvel0: every part answers and every component is up. */
export function setupStatus(over: Rec = {}): Rec {
  return {
    sampled_at: 1_760_000_000,
    cached: false,
    age_s: 0,
    context: { state: "ok" },
    ports: portsPart([5000, 7881, 8000, 8100]),
    gpu: gpuPart(),
    gpu_locks: lockPart(),
    live: livePart(),
    host: { state: "ok" },
    ros: { state: "unknown", detail: "no ROS log folder configured" },
    fr3_health: healthPart(),
    disks: { state: "ok", disks: [] },
    recorder: { state: "unknown", detail: "no recorder folder configured" },
    guard: { state: "ok", quiet: false, robot: [], banner: null },
    ...over,
  };
}

/** The sample as it is: a busy marvel0 (the marker is present, one session
 * updates, one policy unit is active), with whole parts replaced by `over`. */
export function marvel0Status(over: Rec = {}): Rec {
  return { ...structuredClone(sample), ...over };
}

/** A marvel0 with nothing running: the idle answer of the marvel0 status. */
export function quietSetup(): Rec {
  return setupStatus({
    ports: portsPart([7881, 8100]),
    fr3_health: {
      state: "missing",
      detail: "no health file: the monitor is not running",
    },
    gpu_locks: {
      state: "ok",
      locks: [{ label: "levi", state: "free", holders: [] }],
    },
    gpu: gpuPart({ processes: [] }),
  });
}

/** The quiet sample as it is: nothing running, and the idle answer. */
export function quietMarvel0(): Rec {
  return structuredClone(quiet);
}
