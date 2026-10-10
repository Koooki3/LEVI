// Pure rules of the campaign overview (/automatic/campaigns/<id>): what the
// state allows, how an arm is named, and what the page may show while the
// results are blinded. While `blinded` is true the page shows counts and
// codes only; the snapshot type has no success-rate field, and `viewOf` copies
// the named fields only, so a rate the server might add by mistake is never
// rendered.
import type {
  CampaignArmProgress,
  KnownTodoKind,
  CampaignSnapshot,
  CampaignTodo,
} from "./wizard-types";

/** States in which a pause can be asked for (the campaign is moving). */
const PAUSABLE = new Set([
  "SEGMENT_PREPARE",
  "POLICY_STOP",
  "POLICY_START",
  "POLICY_READY",
  "ENV_CONFIRM",
  "ARM_RUNNING",
  "SEGMENT_SEALED",
  "WAIT_HUMAN",
]);
/** States after which the report exists or is being made. */
const REPORT_STATES = new Set(["ANALYZING", "REPORTED"]);
const END_STATES = new Set(["ANALYZING", "REPORTED", "ABORTED"]);
/** States in which no controller will ever be needed again. */
const ENDED_STATES = new Set(["REPORTED", "ABORTED"]);

export type ArmRow = {
  id: string | null; // shown only once the results are unblinded
  code: string;
  done: number;
  remaining: number;
  deviated: number;
  discarded: number;
  /** Guided: episodes whose layout card is not confirmed (0 when unknown). */
  unconfirmed: number;
  total: number;
};

export type CampaignView = {
  id: string;
  state: string;
  blinded: boolean;
  paused: boolean;
  canPause: boolean;
  canResume: boolean;
  finished: boolean;
  reportReady: boolean;
  arms: ArmRow[];
  segment: { no: number; total: number; armCode: string };
  todo: CampaignTodo | null;
  faults: number;
  fused: boolean;
  etaSeconds: number | null;
  done: number;
  total: number;
  executionMode: "guided" | "dry_run" | null;
  /** False only when the server says the controller is not running. */
  controllerDown: boolean;
  /** The controller is gone and the campaign has not ended: it can be
   * attached again. */
  canAttach: boolean;
  /** Looks at the results before the end; above 0 the conclusion is
   * exploratory. */
  peeks: number;
  waitReason: string | null;
  childRunId: string | null;
};

function armRow(arm: CampaignArmProgress, blinded: boolean): ArmRow {
  return {
    id: blinded ? null : arm.id,
    code: arm.code,
    done: arm.done,
    remaining: arm.remaining,
    deviated: arm.deviated,
    discarded: arm.discarded,
    unconfirmed: arm.unconfirmed ?? 0,
    total: arm.done + arm.remaining,
  };
}

export function viewOf(snapshot: CampaignSnapshot): CampaignView {
  const blinded = snapshot.blinded !== false; // anything but an explicit false is blind
  const arms = (snapshot.arms ?? []).map((arm) => armRow(arm, blinded));
  const safety = snapshot.safety ?? { faults: 0, fused: false };
  const segment = snapshot.segment ?? { no: 0, total: 0, arm_code: "" };
  const state = snapshot.state;
  const paused = state === "PAUSED";
  const controllerDown = snapshot.controller?.alive === false;
  const mode = snapshot.execution_mode;
  return {
    id: snapshot.id,
    state,
    blinded,
    paused,
    canPause: PAUSABLE.has(state) && !safety.fused,
    canResume: paused && !safety.fused,
    finished: END_STATES.has(state),
    reportReady: REPORT_STATES.has(state),
    arms,
    segment: {
      no: segment.no,
      total: segment.total,
      armCode: segment.arm_code,
    },
    todo: snapshot.todo && snapshot.todo.kind ? { ...snapshot.todo } : null,
    faults: safety.faults,
    fused: safety.fused,
    etaSeconds:
      typeof snapshot.eta_s === "number" && snapshot.eta_s >= 0
        ? snapshot.eta_s
        : null,
    done: arms.reduce((n, a) => n + a.done, 0),
    total: arms.reduce((n, a) => n + a.total, 0),
    executionMode:
      mode === "guided" ? "guided" : mode === "dry_run" ? "dry_run" : null,
    controllerDown,
    canAttach: controllerDown && !ENDED_STATES.has(state),
    peeks:
      typeof snapshot.peeks === "number" && snapshot.peeks > 0
        ? snapshot.peeks
        : 0,
    waitReason: snapshot.wait_reason ?? null,
    childRunId: snapshot.child_run_id ?? null,
  };
}

/** "5 min", "1 h 20 min": the remaining time in the two largest units. */
export function formatEta(seconds: number): string {
  const minutes = Math.max(1, Math.round(seconds / 60));
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

export const STATE_TONE: Record<
  string,
  "info" | "warning" | "danger" | "success" | "neutral"
> = {
  PAUSED: "warning",
  WAIT_HUMAN: "warning",
  FAULT_LOCKED: "danger",
  ABORTED: "danger",
  REPORTED: "success",
  ANALYZING: "info",
};

const KNOWN_TODO = new Set<string>([
  "switch_policy",
  "place_cards",
  "segment_done",
  "recover_run",
]);

/** Whether the page knows how to show and confirm this kind of to-do; any
 * other kind gets a text-only card with no way to confirm. */
export function isKnownTodo(
  todo: CampaignTodo,
): todo is CampaignTodo & { kind: KnownTodoKind } {
  return todo.kind != null && KNOWN_TODO.has(todo.kind);
}

/** What identifies a step of the campaign. The server renews the challenge of
 * the same step about every minute, so the challenge is not part of it: a card
 * keyed by this keeps the person's ticks while the challenge changes, and
 * starts afresh only when the step (kind, segment, arm, reason) changes. */
export function todoStepKey(todo: CampaignTodo): string {
  return [
    todo.kind ?? "",
    todo.segment ?? "",
    todo.arm_code ?? "",
    todo.reason ?? "",
  ].join(":");
}

/** The kind of confirmation request a todo needs (`/confirm` takes three). */
export function confirmKindOf(
  todo: CampaignTodo,
): "switch_policy" | "env" | "segment_done" | null {
  if (todo.kind === "switch_policy") return "switch_policy";
  if (todo.kind === "segment_done") return "segment_done";
  if (todo.kind === "place_cards" || todo.kind === "recover_run") return "env";
  return null;
}

/** Catalogue keys of the states and of the reasons a campaign waits; a code
 * the table does not know falls back to a general sentence, never the raw
 * code. */
export const STATE_KEYS: Record<string, string> = {
  DRAFT: "automatic.campaign.state.DRAFT",
  PLANNED: "automatic.campaign.state.PLANNED",
  SEGMENT_PREPARE: "automatic.campaign.state.SEGMENT_PREPARE",
  POLICY_STOP: "automatic.campaign.state.POLICY_STOP",
  POLICY_START: "automatic.campaign.state.POLICY_START",
  POLICY_READY: "automatic.campaign.state.POLICY_READY",
  ENV_CONFIRM: "automatic.campaign.state.ENV_CONFIRM",
  ARM_RUNNING: "automatic.campaign.state.ARM_RUNNING",
  SEGMENT_SEALED: "automatic.campaign.state.SEGMENT_SEALED",
  ANALYZING: "automatic.campaign.state.ANALYZING",
  REPORTED: "automatic.campaign.state.REPORTED",
  PAUSED: "automatic.campaign.state.PAUSED",
  WAIT_HUMAN: "automatic.campaign.state.WAIT_HUMAN",
  FAULT_LOCKED: "automatic.campaign.state.FAULT_LOCKED",
  ABORTED: "automatic.campaign.state.ABORTED",
};
export const STATE_OTHER_KEY = "automatic.campaign.state.other";

export const REASON_KEYS: Record<string, string> = {
  child_fault: "automatic.campaign.reason.child_fault",
  child_crashed: "automatic.campaign.reason.child_crashed",
  child_missing: "automatic.campaign.reason.child_missing",
  stop_rule_faults: "automatic.campaign.reason.stop_rule_faults",
  stop_rule_interventions: "automatic.campaign.reason.stop_rule_interventions",
  recovery_ambiguous: "automatic.campaign.reason.recovery_ambiguous",
  conductor_restarted: "automatic.campaign.reason.conductor_restarted",
  listener_mismatch: "automatic.campaign.reason.listener_mismatch",
  policy_not_ready: "automatic.campaign.reason.policy_not_ready",
  plan_changed: "automatic.campaign.reason.plan_changed",
  foreign_run: "automatic.campaign.reason.foreign_run",
  launch_failed: "automatic.campaign.reason.launch_failed",
  segment_short: "automatic.campaign.reason.segment_short",
  paused: "automatic.campaign.reason.paused",
};
export const REASON_OTHER_KEY = "automatic.campaign.reason.other";

export function stateKey(state: string): string {
  return STATE_KEYS[state] ?? STATE_OTHER_KEY;
}
export function reasonKey(reason: string | null | undefined): string {
  return (reason && REASON_KEYS[reason]) || REASON_OTHER_KEY;
}

/** Which of the per-episode card choices a person can make. `NONE` means
 * "no card / the episode deviated" (sent as `card: null`). */
export const NO_CARD = "__none__";

/** The card choices of an episode: its suggestion first, then the other
 * cards the page knows of, in order, without repeats. */
export function cardChoices(
  candidate: string | null,
  known: readonly string[],
): string[] {
  const out: string[] = [];
  for (const card of [candidate, ...known])
    if (card && !out.includes(card)) out.push(card);
  return out;
}

/** The unblind request is allowed only while results are blind and the
 * campaign has not ended without a report. */
export function canUnblind(view: CampaignView): boolean {
  return view.blinded;
}
