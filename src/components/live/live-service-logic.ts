// The "Background service" block of the live page: shapes of
// `GET /api/levi/live/service`, its start/stop calls and the operation they
// start (levi/live/service_control.py), and the pure rules the block follows.
// The browser never holds a token: the same-origin proxy adds it.

export type CheckLevel = "refuse" | "confirm" | "warn" | "ok";

export interface ServiceCheck {
  code: string;
  level: CheckLevel;
  message: string;
}

export interface PortRow {
  port: number;
  role: string;
  state: "free" | "ours" | "foreign" | "unknown";
  pid?: number | null;
  name?: string | null;
}

export interface GpuProcess {
  pid: number;
  name?: string | null;
  memory_mib?: number | null;
}

export interface ServiceOperation {
  operation_id: string;
  action: "start" | "stop" | string;
  request_id?: string;
  /** preparing (the preflight runs), refused, running, done, failed. */
  state: string;
  result?: string | null;
  detail?: string;
  method?: string | null;
  started_at?: number;
  ended_at?: number | null;
}

export interface ServiceStatus {
  enabled: boolean;
  served_by_live_service?: boolean;
  workspace_found?: boolean;
  workspace_name?: string | null;
  holder?: {
    pid?: number | null;
    alive: boolean;
    /** unit | terminal | terminal_once | other | null */
    started_by?: string | null;
    name?: string | null;
    started_at?: number | null;
  };
  unit?: {
    name?: string | null;
    installed?: boolean;
    state?: string | null;
    sub_state?: string | null;
    n_restarts?: number | null;
    flapping?: boolean;
    problem?: string | null;
  };
  ports?: PortRow[];
  gpu?: {
    state?: string;
    lock?: { configured?: boolean; state?: string; pid?: number | null };
    others?: GpuProcess[] | null;
    vllm_state?: string | null;
  };
  evaluation?: { active: boolean | null; count?: number | null };
  start_checks?: ServiceCheck[];
  stop_checks?: ServiceCheck[];
  can_start?: boolean;
  can_stop?: boolean;
  refusals?: string[];
  needs_confirmation?: { start?: string[]; stop?: string[] };
  operation?: ServiceOperation | null;
  confirm?: { start?: string; stop?: string; force_phrase?: string };
  /** Not in the API today: shown folded when the core ever sends it. */
  log_tail?: string[] | null;
}

export const START_PHRASE = "start-live";
export const STOP_PHRASE = "stop-live";
export const FORCE_PHRASE = "stop live during evaluation";
export const CONTROL_SETTING = "LEVI_LIVE_SERVICE_CONTROL=1";

export type ServiceKind = "running" | "starting" | "stopped" | "failed";

const OPERATION_DONE = ["done", "failed", "refused"];

/** Whether an operation is over (its page can stop being polled). */
export function operationOver(op: ServiceOperation | null | undefined) {
  return !!op && OPERATION_DONE.includes(op.state);
}

/** One word for the service: running, starting, stopped, failed. */
export function serviceKind(status: ServiceStatus): ServiceKind {
  const unitState = status.unit?.state ?? "";
  const op = status.operation;
  if (status.holder?.alive) {
    return unitState === "activating" ? "starting" : "running";
  }
  if (unitState === "failed" || status.unit?.flapping) return "failed";
  if (
    unitState === "activating" ||
    (op && op.action === "start" && !operationOver(op))
  )
    return "starting";
  return "stopped";
}

/** Who started the running service, in the words of the page. */
export type StartedBy = "unit" | "terminal" | "terminal_once" | "other" | null;

export function startedBy(status: ServiceStatus): StartedBy {
  if (!status.holder?.alive) return null;
  const by = status.holder.started_by;
  return by === "unit" ||
    by === "terminal" ||
    by === "terminal_once" ||
    by === "other"
    ? by
    : "other";
}

const levelOf = (checks: ServiceCheck[] | undefined, level: CheckLevel) =>
  (checks ?? []).filter((check) => check.level === level);

/** Why a start would be refused (one entry per refusing check). */
export function startRefusals(status: ServiceStatus): ServiceCheck[] {
  return levelOf(status.start_checks, "refuse");
}

export function stopRefusals(status: ServiceStatus): ServiceCheck[] {
  return levelOf(status.stop_checks, "refuse");
}

/** The confirmations a start asks for (`gpu_shared`, `unit_failed`). */
export function startConfirmations(status: ServiceStatus): string[] {
  return status.needs_confirmation?.start ?? [];
}

export function stopConfirmations(status: ServiceStatus): string[] {
  return status.needs_confirmation?.stop ?? [];
}

/** A stop that ends an evaluation's online judgement needs the long phrase. */
export function stopNeedsForce(status: ServiceStatus): boolean {
  return stopConfirmations(status).some(
    (code) => code === "evaluation_active" || code === "evaluation_unknown",
  );
}

export interface StartForm {
  gpuShared: boolean;
  phrase: string;
}

/** Why the Start button is off, or null when it can be pressed. Words, not a
 * tooltip: the page prints the returned key. */
export function startBlock(
  status: ServiceStatus,
  form: StartForm,
  busy: boolean,
): string | null {
  if (busy) return "liveService.block.busy";
  if (!status.enabled) return "liveService.block.disabled";
  if (status.served_by_live_service) return "liveService.block.servedByLive";
  if (startRefusals(status).length) return "liveService.block.refused";
  if (startConfirmations(status).includes("gpu_shared") && !form.gpuShared)
    return "liveService.block.needGpuConfirm";
  if (form.phrase.trim() !== (status.confirm?.start ?? START_PHRASE))
    return "liveService.block.needPhrase";
  return null;
}

export function stopBlock(
  status: ServiceStatus,
  phrase: string,
  forcePhrase: string,
  busy: boolean,
): string | null {
  if (busy) return "liveService.block.busy";
  if (!status.enabled) return "liveService.block.disabled";
  if (status.served_by_live_service) return "liveService.block.servedByLive";
  if (stopRefusals(status).length) return "liveService.block.refused";
  if (phrase.trim() !== (status.confirm?.stop ?? STOP_PHRASE))
    return "liveService.block.needPhrase";
  if (
    stopNeedsForce(status) &&
    forcePhrase.trim() !== (status.confirm?.force_phrase ?? FORCE_PHRASE)
  )
    return "liveService.block.needForcePhrase";
  return null;
}

/** The body of a start call. `reset_failed` is sent when the unit is in the
 * failed state: pressing "Reset and start" is that confirmation. */
export function startBody(
  status: ServiceStatus,
  form: StartForm,
  requestId: string,
) {
  return {
    request_id: requestId,
    confirm: status.confirm?.start ?? START_PHRASE,
    confirm_gpu_shared: form.gpuShared,
    reset_failed: startConfirmations(status).includes("unit_failed"),
  };
}

export function stopBody(
  status: ServiceStatus,
  forcePhrase: string,
  requestId: string,
) {
  return {
    request_id: requestId,
    confirm: status.confirm?.stop ?? STOP_PHRASE,
    force_phrase: stopNeedsForce(status) ? forcePhrase.trim() : "",
  };
}

/** One request id per intent: pressing the same button again while its
 * call is unanswered, or while its operation runs, sends the same id (the
 * core gives the same operation back). A refusal or a finished operation
 * frees it: the next press is a new request. */
export class RequestIds {
  private held: { action: string; id: string } | null = null;
  constructor(private make: () => string = () => crypto.randomUUID()) {}
  next(action: string): string {
    if (!this.held || this.held.action !== action)
      this.held = { action, id: this.make() };
    return this.held.id;
  }
  release(): void {
    this.held = null;
  }
}

// --- the words -----------------------------------------------------------------

/** Catalogue keys of the core's check and error codes. */
export const CODE_TEXT: Record<string, string> = {
  served_by_live_service: "liveService.code.servedByLive",
  unit_not_installed: "liveService.code.unitNotInstalled",
  unit_name_invalid: "liveService.code.unitInvalid",
  unit_is_self: "liveService.code.unitInvalid",
  unit_not_live: "liveService.code.unitInvalid",
  no_live_workspace: "liveService.code.noWorkspace",
  not_writable: "liveService.code.notWritable",
  terminal_instance: "liveService.code.terminalInstance",
  already_running: "liveService.code.alreadyRunning",
  unit_flapping: "liveService.code.unitFlapping",
  unit_busy: "liveService.code.unitBusy",
  port_foreign: "liveService.code.portForeign",
  ports_unknown: "liveService.code.portsUnknown",
  systemd_unavailable: "liveService.code.systemdUnavailable",
  systemd_timeout: "liveService.code.systemdUnavailable",
  permission_denied: "liveService.code.permissionDenied",
  not_running: "liveService.code.notRunning",
  evaluation_active: "liveService.code.evaluationActive",
  evaluation_unknown: "liveService.code.evaluationUnknown",
  gpu_shared: "liveService.code.gpuShared",
  gpu_shared_unconfirmed: "liveService.code.gpuShared",
  unit_failed: "liveService.code.unitFailed",
  confirm_required: "liveService.code.confirmRequired",
  busy: "liveService.code.busy",
  disabled: "liveService.code.disabled",
};

/** The catalogue key for a code, or null (then the core's sentence is shown). */
export function codeKey(code: string): string | null {
  return CODE_TEXT[code] ?? null;
}

export const KIND_TEXT: Record<ServiceKind, string> = {
  running: "liveService.state.running",
  starting: "liveService.state.starting",
  stopped: "liveService.state.stopped",
  failed: "liveService.state.failed",
};

export const OPERATION_TEXT: Record<string, string> = {
  preparing: "liveService.op.preparing",
  running: "liveService.op.running",
  done: "liveService.op.done",
  failed: "liveService.op.failed",
  refused: "liveService.op.refused",
};

export const PORT_TEXT: Record<string, string> = {
  free: "liveService.port.free",
  ours: "liveService.port.ours",
  foreign: "liveService.port.foreign",
  unknown: "liveService.port.unknown",
};

export const ROLE_TEXT: Record<string, string> = {
  core: "liveService.role.core",
  online: "liveService.role.online",
  vllm: "liveService.role.vllm",
  ui: "liveService.role.ui",
};
