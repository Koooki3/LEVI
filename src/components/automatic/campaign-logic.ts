// Pure rules of the campaign overview (/automatic/campaigns/<id>): what the
// state allows, how an arm is named, and what the page may show while the
// results are blinded. While `blinded` is true the page shows counts and
// codes only; the snapshot type has no success-rate field, and `viewOf` copies
// the named fields only, so a rate the server might add by mistake is never
// rendered.
import type {
  CampaignArmProgress,
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

export type ArmRow = {
  id: string | null; // shown only once the results are unblinded
  code: string;
  done: number;
  remaining: number;
  deviated: number;
  discarded: number;
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
};

function armRow(arm: CampaignArmProgress, blinded: boolean): ArmRow {
  return {
    id: blinded ? null : arm.id,
    code: arm.code,
    done: arm.done,
    remaining: arm.remaining,
    deviated: arm.deviated,
    discarded: arm.discarded,
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

/** The kind of confirmation request a todo needs (`/confirm` takes two). */
export function confirmKindOf(
  todo: CampaignTodo,
): "switch_policy" | "env" | null {
  if (todo.kind === "switch_policy") return "switch_policy";
  if (todo.kind === "place_cards" || todo.kind === "recover_run") return "env";
  return null;
}

/** The unblind request is allowed only while results are blind and the
 * campaign has not ended without a report. */
export function canUnblind(view: CampaignView): boolean {
  return view.blinded;
}
