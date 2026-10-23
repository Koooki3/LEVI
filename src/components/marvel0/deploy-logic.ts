import {
  inferenceMode,
  validOverlap,
  type InferenceMode,
} from "./inference-mode";
// Pure mapping for the Policy server card of the marvel0: the answer of
// `GET /api/levi/marvel0/deploy` in, one view model out; the requests of the two
// writes; the words of what the server answered. No React and no fetching, so
// every rule can be tested on its own.
//
// Nothing here decides whether a deploy or stop may happen: the server does,
// and refuses with a stable code. The page only keeps its buttons off when the
// server says it would refuse, and never shows an answer it could not read as
// current: a part that is missing or malformed is unknown, not idle, and a
// state the page does not know is "unknown". Text that came from a server is
// worded from the catalogue when its code is known, and shown as written
// (English) when not.
import type { ApiFailure } from "./deploy-api";
import type { ModelRow, ModelsView, SiteView } from "./catalog-logic";
import { sayFailure } from "./marvel0-failure";
import { bool, list, num, rec, str, type Worded } from "./marvel0-json";
import {
  MARVEL0_POLL_MAX_MS,
  MARVEL0_POLL_MS,
  type Say,
} from "./marvel0-logic";

export const DEPLOY_SCHEMA = "levi.marvel0.deploy.v1";
export const DEPLOY_RESULT_SCHEMA = "levi.marvel0.deploy_result.v1";
export const STOP_RESULT_SCHEMA = "levi.marvel0.stop_result.v1";
export const LOG_SCHEMA = "levi.marvel0.deploy_log.v1";
/** How many lines of the unit's log the card asks for. */
export const LOG_LINES = 80;

export const DEPLOY_STATES = [
  "idle",
  "starting",
  "ready",
  "failed",
  "dead",
  "unknown",
  "foreign_listener",
  "stopping",
] as const;
export type DeployState = (typeof DEPLOY_STATES)[number];

/** Catalogue key of the badge word of each state. */
export const STATE_KEY: Record<DeployState, string> = {
  idle: "marvel0.deploy.state.idle",
  starting: "marvel0.deploy.state.starting",
  ready: "marvel0.deploy.state.ready",
  failed: "marvel0.deploy.state.failed",
  dead: "marvel0.deploy.state.dead",
  unknown: "marvel0.deploy.state.unknown",
  foreign_listener: "marvel0.deploy.state.foreign_listener",
  stopping: "marvel0.deploy.state.stopping",
};
/** Only a server that is ready is a success; "idle" says nothing was seen. */
export const STATE_TONE: Record<
  DeployState,
  "success" | "danger" | "warning" | "neutral"
> = {
  idle: "neutral",
  starting: "warning",
  ready: "success",
  failed: "danger",
  dead: "danger",
  unknown: "neutral",
  foreign_listener: "warning",
  stopping: "warning",
};
const SUMMARY_KEY: Record<DeployState, string> = {
  idle: "marvel0.deploy.summary.idle",
  starting: "marvel0.deploy.summary.starting",
  ready: "marvel0.deploy.summary.ready",
  failed: "marvel0.deploy.summary.failed",
  dead: "marvel0.deploy.summary.dead",
  unknown: "marvel0.deploy.summary.unknown",
  foreign_listener: "marvel0.deploy.summary.foreign_listener",
  stopping: "marvel0.deploy.summary.stopping",
};

// -- the answer

export type CheckLevel = "ok" | "refuse" | "unknown";

export interface DeployCheck {
  code: string;
  level: CheckLevel;
  /** The server's sentence, English. */
  text: string;
  params: Record<string, unknown>;
}

export interface PresetFact {
  fraction: number;
  requiredMib: number | null;
  enough: boolean | null;
}

export interface DeployInfo {
  requestId: string | null;
  unit: string | null;
  modelId: string | null;
  config: string | null;
  rtc: boolean | null;
  inferenceMode?: InferenceMode;
  vlashOverlapSteps?: number | null;
  fraction: number | null;
  /** The guidance scale of the deploy; null when it has none. */
  cfgScale: number | null;
  phase: string | null;
  startedAt: string | null;
  ageS: number | null;
}

export interface DeployStatus {
  state: DeployState;
  reason: { code: string; text: string } | null;
  deploy: DeployInfo | null;
  /** systemd's state of the unit, as written; null when it was not read. */
  unit: { state: string | null; sub: string | null };
  listener: {
    port: number | null;
    present: boolean | null;
    ours: boolean | null;
    established: number | null;
  };
  readiness: {
    normStats: boolean | null;
    rtcEnabled: boolean | null;
    rtcWarmup: boolean | null;
    vlashEnabled?: boolean | null;
    cfg: boolean | null;
    listening: boolean | null;
  };
  failure: { kind: string; text: string } | null;
  gpu: {
    ok: boolean;
    totalMib: number | null;
    usedMib: number | null;
    freeMib: number | null;
    holdsMib: number | null;
  };
  vram: {
    marginMib: number | null;
    presets: PresetFact[];
    hint: string | null;
  };
  checks: DeployCheck[];
  canDeploy: boolean;
  canStop: boolean;
  stopBlockers: string[];
  /** A running server holds the card's memory, so live annotation cannot start. */
  liveBlocked: boolean;
  estop: { keepHand: boolean };
  operation: { action: string; requestId: string | null } | null;
  notes: Array<{ code: string; text: string }>;
  confirm: { deploy: string; stop: string; clientOverride: string };
}

const isState = (value: unknown): value is DeployState =>
  typeof value === "string" &&
  (DEPLOY_STATES as readonly string[]).includes(value);

function readCheck(entry: unknown): DeployCheck | null {
  const item = rec(entry);
  const code = str(item?.code, 40);
  if (!item || !code) return null;
  const level = item.level;
  return {
    code,
    level: level === "ok" || level === "refuse" ? level : "unknown",
    text: str(item.text) ?? "",
    params: rec(item.params) ?? {},
  };
}

function readInfo(part: unknown): DeployInfo | null {
  const item = rec(part);
  if (!item) return null;
  return {
    requestId: str(item.request_id, 80),
    unit: str(item.unit, 80),
    modelId: str(item.model_id, 120),
    config: str(item.config, 120),
    rtc: inferenceMode(item.inference_mode, bool(item.rtc)) === "rtc",
    inferenceMode: inferenceMode(item.inference_mode, bool(item.rtc)),
    vlashOverlapSteps: num(item.vlash_overlap_steps),
    fraction: num(item.fraction),
    cfgScale: num(item.cfg_guidance_scale),
    phase: str(item.phase, 40),
    startedAt: str(item.started_at, 40),
    ageS: num(item.age_s),
  };
}

function codeAndText(part: unknown, field: "code" | "kind") {
  const item = rec(part);
  const code = str(item?.[field], 60);
  return item && code ? { code, text: str(item.text) ?? "" } : null;
}

/** The answer of `GET /api/levi/marvel0/deploy`, or null when it is not in the
 * format this page was written for. A deploy or a stop is offered only when
 * the server says `true`; a missing e-stop part means the hand stays on it. */
export function readDeploy(data: unknown): DeployStatus | null {
  const body = rec(data);
  if (!body || body.schema !== DEPLOY_SCHEMA) return null;
  const listener = rec(body.listener);
  const readiness = rec(body.readiness);
  const gpu = rec(body.gpu);
  const vram = rec(body.vram);
  const confirm = rec(body.confirm);
  const estop = rec(body.estop);
  const operation = rec(body.operation);
  const failed = codeAndText(body.failure, "kind");
  const reason = codeAndText(body.reason, "code");
  return {
    state: isState(body.state) ? body.state : "unknown",
    reason,
    deploy: readInfo(body.deploy),
    unit: {
      state: str(rec(body.unit)?.state, 40),
      sub: str(rec(body.unit)?.sub, 40),
    },
    listener: {
      port: num(listener?.port),
      present: bool(listener?.present),
      ours: bool(listener?.ours),
      established: num(listener?.established),
    },
    readiness: {
      normStats: bool(readiness?.norm_stats),
      rtcEnabled: bool(readiness?.rtc_enabled),
      rtcWarmup: bool(readiness?.rtc_warmup),
      vlashEnabled: bool(readiness?.vlash_enabled),
      cfg: bool(readiness?.cfg),
      listening: bool(readiness?.listening),
    },
    failure: failed && { kind: failed.code, text: failed.text },
    gpu: {
      ok: gpu?.state === "ok",
      totalMib: num(gpu?.total_mib),
      usedMib: num(gpu?.used_mib),
      freeMib: num(gpu?.free_mib),
      holdsMib: num(gpu?.server_holds_mib),
    },
    vram: {
      marginMib: num(vram?.margin_mib),
      presets: list(vram?.presets).flatMap((entry) => {
        const item = rec(entry);
        const fraction = num(item?.fraction);
        return item && fraction !== null
          ? [
              {
                fraction,
                requiredMib: num(item.required_mib),
                enough: bool(item.enough),
              },
            ]
          : [];
      }),
      hint: str(vram?.hint),
    },
    checks: list(body.checks)
      .map(readCheck)
      .filter((check): check is DeployCheck => check !== null),
    canDeploy: body.can_deploy === true,
    canStop: body.can_stop === true,
    stopBlockers: list(body.stop_blockers)
      .map((entry) => str(entry, 40))
      .filter((entry): entry is string => entry !== null),
    liveBlocked: rec(body.live_annotation)?.blocked === true,
    estop: { keepHand: estop?.keep_hand === false ? false : true },
    operation: operation
      ? {
          action: str(operation.action, 20) ?? "",
          requestId: str(operation.request_id, 80),
        }
      : null,
    notes: list(body.notes).flatMap((entry) => {
      const item = rec(entry);
      const code = str(item?.code, 60);
      return item && code ? [{ code, text: str(item.text) ?? "" }] : [];
    }),
    confirm: {
      deploy: str(confirm?.deploy, 80) ?? "deploy {model_id}",
      stop: str(confirm?.stop, 80) ?? "stop policy server",
      clientOverride:
        str(confirm?.client_override, 80) ?? "stop with client connected",
    },
  };
}

// -- words

const port = (status: DeployStatus): string | number =>
  status.listener.port ?? "?";

/** One sentence for the state. */
export function stateSay(status: DeployStatus): Say {
  return { key: SUMMARY_KEY[status.state], params: { port: port(status) } };
}

const REASON_KEY: Record<string, string> = {
  net_unreadable: "marvel0.deploy.reason.net_unreadable",
  systemd_unreadable: "marvel0.deploy.reason.systemd_unreadable",
  record_unreadable: "marvel0.deploy.reason.record_unreadable",
  hand_started: "marvel0.deploy.reason.hand_started",
  listener_not_ours: "marvel0.deploy.reason.listener_not_ours",
  owner_unknown: "marvel0.deploy.reason.owner_unknown",
  exited_after_ready: "marvel0.deploy.reason.exited_after_ready",
  stopped_elsewhere: "marvel0.deploy.reason.stopped_elsewhere",
  unclassified: "marvel0.deploy.reason.unclassified",
  request_incomplete: "marvel0.deploy.reason.request_incomplete",
};
const FAILURE_KEY: Record<string, string> = {
  xla_oom: "marvel0.deploy.failure.xla_oom",
  argparse: "marvel0.deploy.failure.argparse",
  address_in_use: "marvel0.deploy.failure.address_in_use",
  traceback: "marvel0.deploy.failure.traceback",
  rtc_not_ready: "marvel0.deploy.failure.rtc_not_ready",
  vlash_not_ready: "marvel0.deploy.failure.vlash_not_ready",
  cfg_not_confirmed: "marvel0.deploy.failure.cfg_not_confirmed",
  start_timeout: "marvel0.deploy.failure.start_timeout",
  exited: "marvel0.deploy.failure.exited",
  start_failed: "marvel0.deploy.failure.start_failed",
  intent_only: "marvel0.deploy.failure.intent_only",
};

function coded(
  table: Record<string, string>,
  part: { code: string; text: string } | null,
  status: DeployStatus,
): Worded<Say> | null {
  if (!part) return null;
  const key = table[part.code];
  return key
    ? { say: { key, params: { port: port(status) } }, text: part.text }
    : { say: null, text: part.text };
}

/** Why the state is what it is, when the server gave a reason. */
export function reasonSay(status: DeployStatus): Worded<Say> | null {
  return coded(REASON_KEY, status.reason, status);
}

/** What went wrong, when the server says the deploy failed. */
export function failureSay(status: DeployStatus): Worded<Say> | null {
  return coded(
    FAILURE_KEY,
    status.failure && { code: status.failure.kind, text: status.failure.text },
    status,
  );
}

const DEPLOY_SHOWN: DeployState[] = [
  "starting",
  "ready",
  "failed",
  "dead",
  "stopping",
];

/** The record of the deploy the state is about: only while that deploy is
 * starting, running, stopping or has ended badly. An idle marvel0 has a record of
 * the last deploy too (stopped), but its model and log lines are not facts
 * about the marvel0 now. */
export function shownDeploy(status: DeployStatus): DeployInfo | null {
  return DEPLOY_SHOWN.includes(status.state) ? status.deploy : null;
}

export interface ReadinessRow {
  id:
    | "norm_stats"
    | "rtc_enabled"
    | "rtc_warmup"
    | "vlash_enabled"
    | "cfg"
    | "listening";
  /** Catalogue key. */
  label: string;
  /** What the log said: yes, not yet, or null when nothing has been read. */
  value: boolean | null;
}

/** What the unit's log should say before the server counts as ready, for
 * this deploy: the RTC lines only for an RTC deploy, the guidance line only
 * for a deploy with a guidance scale. */
export function readinessRows(status: DeployStatus): ReadinessRow[] {
  const deploy = shownDeploy(status);
  if (!deploy) return [];
  const r = status.readiness;
  const rows: ReadinessRow[] = [
    {
      id: "norm_stats",
      label: "marvel0.deploy.ready.norm_stats",
      value: r.normStats,
    },
  ];
  if (deploy.rtc === true)
    rows.push(
      {
        id: "rtc_enabled",
        label: "marvel0.deploy.ready.rtc_enabled",
        value: r.rtcEnabled,
      },
      {
        id: "rtc_warmup",
        label: "marvel0.deploy.ready.rtc_warmup",
        value: r.rtcWarmup,
      },
    );
  if (deploy.inferenceMode === "vlash")
    rows.push({
      id: "vlash_enabled",
      label: "marvel0.deploy.ready.vlash_enabled",
      value: r.vlashEnabled ?? null,
    });
  if (deploy.cfgScale !== null)
    rows.push({ id: "cfg", label: "marvel0.deploy.ready.cfg", value: r.cfg });
  rows.push({
    id: "listening",
    label: "marvel0.deploy.ready.listening",
    value: r.listening,
  });
  return rows;
}

export interface PresetRow {
  fraction: number;
  /** "0.30". */
  label: string;
  requiredMib: number | null;
  /** The memory the fraction reserves: what is required less the margin. */
  reservedMib: number | null;
  marginMib: number | null;
  freeMib: number | null;
  enough: boolean | null;
  /** Fractions above the first need the live vLLM asleep or stopped. */
  liveNote: boolean;
  say: Say;
}

const mib = (value: number | null): string | number => value ?? "?";

/** The arithmetic of each fraction as the server did it: what it needs, what
 * is free, and whether that is enough. */
export function presetRows(status: DeployStatus): PresetRow[] {
  const margin = status.vram.marginMib;
  const free = status.gpu.freeMib;
  return status.vram.presets.map((preset, index) => {
    const required = preset.requiredMib;
    const reserved =
      required !== null && margin !== null ? required - margin : null;
    const params = {
      required: mib(required),
      reserved: mib(reserved),
      margin: mib(margin),
      free: mib(free),
    };
    const say: Say =
      required === null || preset.enough === null
        ? { key: "marvel0.deploy.preset.unknown" }
        : {
            key: preset.enough
              ? "marvel0.deploy.preset.enough"
              : "marvel0.deploy.preset.short",
            params,
          };
    return {
      fraction: preset.fraction,
      label: preset.fraction.toFixed(2),
      requiredMib: required,
      reservedMib: reserved,
      marginMib: margin,
      freeMib: free,
      enough: required === null ? null : preset.enough,
      liveNote: index > 0,
      say,
    };
  });
}

/** The line about a running server holding the card's memory. */
export function liveSay(status: DeployStatus): Say | null {
  if (!status.liveBlocked) return null;
  const held = status.gpu.holdsMib;
  return held === null
    ? { key: "marvel0.deploy.live_blocked_unknown" }
    : { key: "marvel0.deploy.live_blocked", params: { n: held } };
}

// -- the checks before a deploy

const CHECK_TITLE: Record<string, string> = {
  state_dir: "marvel0.deploy.check.state_dir.title",
  net_table: "marvel0.deploy.check.net_table.title",
  port_free: "marvel0.deploy.check.port_free.title",
  no_client: "marvel0.deploy.check.no_client.title",
  policy_units: "marvel0.deploy.check.policy_units.title",
  arm_lock: "marvel0.deploy.check.arm_lock.title",
  gpu_lock: "marvel0.deploy.check.gpu_lock.title",
  gpu_readable: "marvel0.deploy.check.gpu_readable.title",
  vram: "marvel0.deploy.check.vram.title",
};
const CHECK_OK: Record<string, string> = {
  state_dir: "marvel0.deploy.check.state_dir.ok",
  net_table: "marvel0.deploy.check.net_table.ok",
  port_free: "marvel0.deploy.check.port_free.ok",
  no_client: "marvel0.deploy.check.no_client.ok",
  policy_units: "marvel0.deploy.check.policy_units.ok",
  arm_lock: "marvel0.deploy.check.arm_lock.ok",
  gpu_lock: "marvel0.deploy.check.gpu_lock.ok",
  gpu_readable: "marvel0.deploy.check.gpu_readable.ok",
  vram: "marvel0.deploy.check.vram.ok",
};
/** The unit name prefix of this page (levi/marvel0/record.py UNIT_RE). */
const OWN_UNIT =
  /^levi-aeri-policy-manual-marvel0-[a-z0-9][a-z0-9-]{0,35}(\.service)?$/;

/** The refusal a check stands for, by its level: the very refusal a deploy
 * would answer with, so a check reads as that refusal does. */
function refusalOf(check: DeployCheck): string | null {
  const refuse = check.level === "refuse";
  switch (check.code) {
    case "state_dir":
      return "state_dir_unusable";
    case "net_table":
      return "net_table_unreadable";
    case "port_free":
      return !refuse
        ? "net_table_unreadable"
        : check.params.port === undefined
          ? "already_running"
          : "port_taken";
    case "no_client":
      return refuse ? "client_connected" : "net_table_unreadable";
    case "policy_units": {
      if (!refuse) return "systemd_unreadable";
      const units = list(check.params.units);
      return units.length > 0 &&
        units.every((unit) => typeof unit === "string" && OWN_UNIT.test(unit))
        ? "already_running"
        : "policy_unit_exists";
    }
    case "arm_lock":
      return refuse ? "arm_lock_held" : "arm_lock_unknown";

    case "gpu_lock":
      return refuse ? "gpu_lock_foreign" : null;
    case "gpu_readable":
      return "gpu_unreadable";
    case "vram":
      return refuse ? "gpu_insufficient" : "gpu_unreadable";
    default:
      return null;
  }
}

function failureOf(code: string, extra: Record<string, unknown>): ApiFailure {
  return {
    ok: false,
    status: 0,
    code,
    message: "",
    extra,
    field: null,
    fields: [],
  };
}

export interface CheckRow {
  code: string;
  /** Catalogue key of the check's name; null for a check the page does not
   * know. */
  title: string | null;
  level: CheckLevel;
  say: Worded<Say>;
}

/** The checks the server ran before a deploy, each in words. */
export function checkRows(status: DeployStatus): CheckRow[] {
  return status.checks.map((check) => {
    const title = CHECK_TITLE[check.code] ?? null;
    const known = title !== null;
    let say: Worded<Say> = { say: null, text: check.text };
    if (known && check.level === "ok")
      say = { say: { key: CHECK_OK[check.code] }, text: check.text };
    else if (known) {
      const code = refusalOf(check);
      if (code)
        say = {
          ...sayFailure(
            failureOf(code, {
              port: status.listener.port,
              ...check.params,
            }),
          ),
          text: check.text,
        };
      else if (check.code === "gpu_lock")
        say = {
          say: { key: "marvel0.deploy.check.gpu_lock.unknown" },
          text: check.text,
        };
    }
    return { code: check.code, title, level: check.level, say };
  });
}

const NOTE_KEY: Record<string, string> = {
  source_hash_unmanifested: "marvel0.deploy.note.source_hash_unmanifested",
};

/** A note of the server about the deploy. */
export function noteSay(note: { code: string; text: string }): Worded<Say> {
  const key = NOTE_KEY[note.code];
  return key
    ? { say: { key }, text: note.text }
    : { say: null, text: note.text };
}

// -- the form and its requests

/** A fresh id for one attempt of a write. `randomUUID` needs a secure context
 * (https or localhost); `getRandomValues` does not, and the id only has to be
 * 8 to 80 characters of A-Z a-z 0-9 . _ : - that nobody else uses. */
export function newRequestId(): string {
  const crypto = globalThis.crypto;
  if (typeof crypto?.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
}

export interface DeployForm {
  modelId: string;
  rtc: boolean;
  inferenceMode?: InferenceMode;
  vlashOverlapSteps?: string;
  /** One of the site's presets; null when there is none. */
  fraction: number | null;
  /** The guidance scale as typed; empty is the server's default. */
  scale: string;
  /** The person accepts a model without a recorded real-GPU check. */
  acceptUnverified: boolean;
}

const sameFraction = (a: number, b: number) =>
  Math.round(a * 10_000) === Math.round(b * 10_000);

/** The form as the page opens: the first model, the server's RTC and fraction
 * defaults when the site offers that fraction. */
export function initialForm(models: ModelsView, site: SiteView): DeployForm {
  const presets = site.gpu.presets;
  const wanted = models.defaults.fraction;
  const fraction =
    presets.find((preset) => wanted !== null && sameFraction(preset, wanted)) ??
    presets[0] ??
    null;
  return {
    modelId: models.models[0]?.id ?? "",
    rtc: models.defaults.rtc,
    fraction,
    scale: "",
    acceptUnverified: false,
  };
}

/** The form with the person's edits over its opening values, kept to what the
 * page can still offer: a model that left the list falls back to the first, a
 * fraction that left the presets to the opening one (the lists are read again
 * when the tab returns after an error, and the site file may have changed). */
export function resolveForm(
  initial: DeployForm,
  edits: Partial<DeployForm>,
  models: ModelsView,
  site: SiteView,
): DeployForm {
  const form = { ...initial, ...edits };
  if (!models.models.some((model) => model.id === form.modelId))
    form.modelId = models.models[0]?.id ?? "";
  if (
    form.fraction === null ||
    !site.gpu.presets.some((preset) => sameFraction(preset, form.fraction!))
  )
    form.fraction = initial.fraction;
  return form;
}

const DECIMAL = /^(?:\d+\.?\d*|\.\d+)$/;

/** The guidance scale: the model's own value, `null` for the server's default
 * (and for a model without guidance); `error` when what was typed is not a
 * number above 0 and within the site's maximum. */
export function parseScale(
  text: string,
  model: ModelRow,
  site: SiteView | null,
): { value: number | null; error: boolean } {
  const typed = text.trim();
  if (!model.cfgCapable || typed === "") return { value: null, error: false };
  const value = DECIMAL.test(typed) ? Number(typed) : NaN;
  const max = site?.caps.cfgScale.max ?? Infinity;
  return Number.isFinite(value) && value > 0 && value <= max
    ? { value, error: false }
    : { value: null, error: true };
}

/** Why Deploy is off, or null when nothing stops the press. The server still
 * decides; this keeps the button off where it would refuse. */
export function deployBlock(
  status: DeployStatus | null,
  form: DeployForm,
  model: ModelRow | null,
  site: SiteView | null,
  pending: boolean,
): Worded<Say> | null {
  const plain = (say: Say): Worded<Say> => ({ say, text: "" });
  if (!status) return plain({ key: "marvel0.deploy.block.no_state" });
  if (pending || status.operation)
    return plain({ key: "marvel0.deploy.block.operation" });
  if (!["idle", "failed", "dead"].includes(status.state))
    return plain({
      key: "marvel0.deploy.block.state",
      params: { state: { key: STATE_KEY[status.state] } },
    });
  const blocking = checkRows(status).find((row) => row.level !== "ok");
  if (blocking)
    return plain({
      key: "marvel0.deploy.block.check",
      params: { reason: blocking.say.say ?? { raw: blocking.say.text } },
    });
  if (
    inferenceMode(form.inferenceMode, form.rtc) === "vlash" &&
    !validOverlap(form.vlashOverlapSteps)
  )
    return plain({ key: "marvel0.inference.overlap_error" });
  if (!model) return plain({ key: "marvel0.deploy.block.no_model" });
  const row = presetRows(status).find(
    (preset) =>
      form.fraction !== null && sameFraction(preset.fraction, form.fraction),
  );
  if (!row) return plain({ key: "marvel0.deploy.block.no_fraction" });
  if (row.enough === null)
    return plain({ key: "marvel0.deploy.block.gpu_unknown" });
  if (!row.enough)
    return plain({
      key: "marvel0.deploy.block.short",
      params: { fraction: row.label, detail: row.say },
    });
  if (parseScale(form.scale, model, site).error)
    return plain({
      key: "marvel0.deploy.block.scale",
      params: { max: site?.caps.cfgScale.max ?? "?" },
    });
  if (!status.canDeploy) return plain({ key: "marvel0.deploy.block.server" });
  return null;
}

/** The phrase to type for a deploy: the server's, with the model filled in. */
export function deployPhrase(
  status: DeployStatus | null,
  modelId: string,
): string {
  return (status?.confirm.deploy ?? "deploy {model_id}").replace(
    "{model_id}",
    modelId,
  );
}
export const stopPhrase = (status: DeployStatus | null): string =>
  status?.confirm.stop ?? "stop policy server";
export const overridePhrase = (status: DeployStatus | null): string =>
  status?.confirm.clientOverride ?? "stop with client connected";

/** The body of `POST /api/levi/marvel0/deploy`. */
export function deployBody(
  requestId: string,
  form: DeployForm,
  model: ModelRow,
  phrase: string,
  site: SiteView | null = null,
): Record<string, unknown> {
  return {
    request_id: requestId,
    model_id: model.id,
    rtc: inferenceMode(form.inferenceMode, form.rtc) === "rtc",
    inference_mode: inferenceMode(form.inferenceMode, form.rtc),
    ...(inferenceMode(form.inferenceMode, form.rtc) === "vlash"
      ? {
          vlash_overlap_steps: form.vlashOverlapSteps?.trim()
            ? Number(form.vlashOverlapSteps)
            : 2,
        }
      : {}),
    fraction: form.fraction,
    cfg_guidance_scale: parseScale(form.scale, model, site).value,
    accept_unverified: !model.verified && form.acceptUnverified,
    confirm: phrase,
  };
}

/** The body of `POST /api/levi/marvel0/deploy/stop`. The override is sent only
 * when it was typed exactly: the server refuses any other text. */
export function stopBody(
  requestId: string,
  phrase: string,
  typedOverride: string,
  override = "",
): Record<string, unknown> {
  return {
    request_id: requestId,
    confirm: phrase,
    client_override:
      override !== "" && typedOverride === override ? override : "",
  };
}

export interface StopControl {
  /** The server says the unit of this page can be stopped. */
  show: boolean;
  /** Something else listens: nothing to stop here, press Ctrl+C there. */
  byHand: boolean;
  /** Clients connected to the policy port; null when the table was unreadable. */
  clients: number | null;
  /** A deploy or stop is running. */
  operation: boolean;
}

export function stopControl(status: DeployStatus | null): StopControl {
  if (!status)
    return { show: false, byHand: false, clients: null, operation: false };
  return {
    show: status.canStop,
    byHand: status.state === "foreign_listener",
    clients: status.listener.established,
    operation: status.operation !== null,
  };
}

// -- what a write answered

export interface ActionResult {
  tone: "success" | "warning";
  lines: Say[];
}

const incomplete = (body: Record<string, unknown>): ActionResult | null =>
  body.state === "unknown" && rec(body.reason)?.code === "request_incomplete"
    ? { tone: "warning", lines: [{ key: REASON_KEY.request_incomplete }] }
    : null;

/** The words for the answer of an accepted deploy. */
export function deployResult(data: unknown): ActionResult | null {
  const body = rec(data);
  if (!body || body.schema !== DEPLOY_RESULT_SCHEMA) return null;
  const gone = incomplete(body);
  if (gone) return gone;
  if (body.replayed === true)
    return {
      tone: "success",
      lines: [{ key: "marvel0.deploy.result.replayed" }],
    };
  const gpu = rec(body.gpu);
  return {
    tone: "success",
    lines: [
      {
        key: "marvel0.deploy.result.started",
        params: {
          required: mib(num(gpu?.required_mib)),
          free: mib(num(gpu?.free_mib_at_final_check)),
        },
      },
    ],
  };
}

/** The words for the answer of a stop. */
export function stopResult(data: unknown): ActionResult | null {
  const body = rec(data);
  if (!body || body.schema !== STOP_RESULT_SCHEMA) return null;
  const gone = incomplete(body);
  if (gone) return gone;
  let result: ActionResult;
  if (body.already === true)
    result = {
      tone: "success",
      lines: [{ key: "marvel0.deploy.stop_result.already" }],
    };
  else if (body.state === "stopping" || body.stopped !== true)
    result = {
      tone: "warning",
      lines: [{ key: "marvel0.deploy.stop_result.stopping" }],
    };
  else
    result = {
      tone: "success",
      lines: [{ key: "marvel0.deploy.stop_result.stopped" }],
    };
  if (body.client_override_used === true)
    return {
      tone: "warning",
      lines: [...result.lines, { key: "marvel0.deploy.stop_result.forced" }],
    };
  return result;
}

// -- the log

export interface LogView {
  /** The log's file name; null when no deploy has a log yet. */
  logId: string | null;
  sizeBytes: number | null;
  /** The last printable lines as the unit wrote them (English). */
  lines: string[];
}

/** The answer of `GET /api/levi/marvel0/deploy/log`, or null when it is not in the
 * format this page was written for. */
export function readLog(data: unknown): LogView | null {
  const body = rec(data);
  if (!body || body.schema !== LOG_SCHEMA || !Array.isArray(body.lines))
    return null;
  return {
    logId: str(body.log_id, 200),
    sizeBytes: num(body.size_bytes),
    lines: body.lines
      .filter((line): line is string => typeof line === "string")
      .slice(-200)
      .map((line) => line.slice(0, 400)),
  };
}

// -- polling

const LOG_STATES: DeployState[] = ["starting", "failed", "dead", "stopping"];

/** Whether the unit's log is asked for in this round: only a deploy of this
 * page has one, and it matters while the server starts, stops or has ended;
 * while it is ready, only if the person has the log open. */
export function wantLog(status: DeployStatus | null, open: boolean): boolean {
  if (!status || !status.deploy) return false;
  return LOG_STATES.includes(status.state) || open;
}

/** Milliseconds until the next request: `pollMs`, doubling after each failure
 * up to 30 s. */
export function deployDelay(
  failures: number,
  pollMs = MARVEL0_POLL_MS,
): number {
  if (failures <= 0) return pollMs;
  return Math.min(
    Math.max(MARVEL0_POLL_MAX_MS, pollMs),
    pollMs * 2 ** Math.min(failures, 6),
  );
}
