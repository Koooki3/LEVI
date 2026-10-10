// Pure rules of the evaluation wizard (/automatic/new): the form, what each
// step checks, and how the form becomes the requests of
// levi2/design/aeri-ui-api-contract.md. No fetch and no React here, so the
// rules are tested on their own.
import { newRequestId } from "./wizard-api";
import type {
  Capabilities,
  CampaignArmInput,
  CampaignExecutionMode,
  CampaignPlanRequest,
  JobCreate,
  PolicyCheckpoint,
  PoliciesResponse,
  ResetStrategy,
  SceneCheck,
  ScheduleKind,
} from "./wizard-types";

export type WizardMode = "single" | "campaign";

/** Fields are text while a person types, and numbers once validated. */
export type WizardForm = {
  mode: WizardMode;
  // choice
  policyId: string;
  /** Campaign: the checkpoints taken in, in the order they were ticked. */
  armIds: string[];
  /** Campaign: the reference arm's checkpoint id, or "" for none. */
  referenceId: string;
  /** "human" or the id of a reset checkpoint. */
  resetChoice: string;
  operatorAttested: boolean;
  // settings
  instruction: string;
  maxSteps: string;
  episodes: string;
  allowEarlyStop: boolean;
  executionMode: string;
  // campaign
  trialsPerArm: string;
  scheduleKind: ScheduleKind;
  segmentTrials: string;
  seed: string;
  labelBasis: string;
  alpha: string;
  preregistered: boolean;
  /** Campaign: who carries it out (the person's legacy client, or fakes). */
  campaignMode: CampaignExecutionMode;
};

export const HUMAN_RESET = "human";
export const MAX_ARMS = 8;
export const SHORT_RUN_STEPS = 120;
export const INSTRUCTION_MAX = 500;

export const SCHEDULE_KINDS: {
  value: ScheduleKind;
  exploratory: boolean;
  key: string;
}[] = [
  {
    value: "counterbalanced_segments",
    exploratory: false,
    key: "automatic.wizard.schedule.counterbalanced_segments",
  },
  {
    value: "randomized_blocks",
    exploratory: false,
    key: "automatic.wizard.schedule.randomized_blocks",
  },
  {
    value: "latin_square",
    exploratory: false,
    key: "automatic.wizard.schedule.latin_square",
  },
  {
    value: "interleaved",
    exploratory: true,
    key: "automatic.wizard.schedule.interleaved",
  },
  {
    value: "blocked",
    exploratory: true,
    key: "automatic.wizard.schedule.blocked",
  },
];

/** The label bases of a campaign (the report's names; an automatic basis is
 * always called unreviewed, and only the adjudicated one says ground truth). */
export const LABEL_BASES: { value: string; key: string }[] = [
  { value: "operator_label", key: "automatic.wizard.basis.operator_label" },
  {
    value: "adjudicated_ground_truth",
    key: "automatic.wizard.basis.adjudicated_ground_truth",
  },
  {
    value: "adjudicated_then_operator",
    key: "automatic.wizard.basis.adjudicated_then_operator",
  },
  {
    value: "autonomous_verdict",
    key: "automatic.wizard.basis.autonomous_verdict",
  },
  { value: "posthoc_verdict", key: "automatic.wizard.basis.posthoc_verdict" },
];

export function defaultForm(capabilities?: Capabilities | null): WizardForm {
  const d = capabilities?.defaults;
  return {
    mode: "single",
    policyId: "",
    armIds: [],
    referenceId: "",
    resetChoice: HUMAN_RESET,
    // Off by default: attesting the scene needs an initial-state contract in
    // the job, and the wizard has no way to give one yet (see attestAvailable).
    operatorAttested: false,
    instruction: "",
    maxSteps: String(d?.max_steps ?? 400),
    episodes: String(d?.episodes ?? 5),
    allowEarlyStop: true,
    executionMode: "dry_run",
    trialsPerArm: "20",
    scheduleKind: "counterbalanced_segments",
    segmentTrials: "5",
    seed: "7",
    labelBasis: "operator_label",
    alpha: "0.05",
    preregistered: true,
    campaignMode: "guided",
  };
}

/** A whole number from text, or null (empty, fractional, not a number). */
export function wholeNumber(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) ? value : null;
}

export function fractionNumber(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d*\.?\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : null;
}

// --- reset options ---------------------------------------------------------

export type ResetOptions = {
  /** A person resets the scene; only the evaluated policy runs. */
  human: true;
  resetCheckpoints: PolicyCheckpoint[];
  /** Why only the human option exists, when it does (catalogue key). */
  onlyHumanReasonKey: string | null;
};

/** The reset choices the machine offers. `reset_available=false` leaves only
 * the human reset, whatever the list of checkpoints holds. */
export function resetOptions(policies: PoliciesResponse | null): ResetOptions {
  const list = policies?.checkpoints.filter((c) => c.role === "reset") ?? [];
  if (!policies?.reset_available || list.length === 0)
    return {
      human: true,
      resetCheckpoints: [],
      onlyHumanReasonKey: "automatic.wizard.reset.none_available",
    };
  return { human: true, resetCheckpoints: list, onlyHumanReasonKey: null };
}

export function forwardCheckpoints(
  policies: PoliciesResponse | null,
): PolicyCheckpoint[] {
  return policies?.checkpoints.filter((c) => c.role === "forward") ?? [];
}

export function resetStrategyOf(form: WizardForm): ResetStrategy {
  return form.resetChoice === HUMAN_RESET
    ? "human_assisted"
    : "single_reset_policy";
}

/** Whether the person may attest the scene. The server accepts
 * `operator_attested` only for a job with `task.initial_state_spec` (an
 * initial-state contract), and the jobs this wizard writes have none, so the
 * option is not offered; the scene is then judged by the provider. */
export function attestAvailable(): boolean {
  return false;
}

/** The scene check: a reset policy's scene is always judged by the provider;
 * with a human reset the person may attest the scene instead, when the job
 * has a contract to attest against. */
export function sceneCheckOf(form: WizardForm): SceneCheck {
  return attestAvailable() &&
    form.resetChoice === HUMAN_RESET &&
    form.operatorAttested
    ? "operator_attested"
    : "provider";
}

// --- validation ------------------------------------------------------------

export type Problems = Record<string, string>;

export function validateChoice(
  form: WizardForm,
  policies: PoliciesResponse | null,
): Problems {
  const problems: Problems = {};
  const forward = forwardCheckpoints(policies);
  if (form.mode === "single") {
    if (!form.policyId) problems.policy = "automatic.wizard.err.policy_missing";
    else if (!forward.some((c) => c.id === form.policyId))
      problems.policy = "automatic.wizard.err.policy_unknown";
  } else {
    const chosen = form.armIds;
    if (chosen.length < 2) problems.arms = "automatic.wizard.err.arms_min";
    else if (chosen.length > MAX_ARMS)
      problems.arms = "automatic.wizard.err.arms_max";
    else if (chosen.some((id) => !forward.some((c) => c.id === id)))
      problems.arms = "automatic.wizard.err.policy_unknown";
    if (form.referenceId && !chosen.includes(form.referenceId))
      problems.reference = "automatic.wizard.err.reference_not_arm";
  }
  const options = resetOptions(policies);
  if (
    form.resetChoice !== HUMAN_RESET &&
    !options.resetCheckpoints.some((c) => c.id === form.resetChoice)
  )
    problems.reset = "automatic.wizard.err.reset_unavailable";
  return problems;
}

export function validateSettings(form: WizardForm): Problems {
  const problems: Problems = {};
  const instruction = form.instruction.trim();
  if (!instruction) problems.instruction = "automatic.wizard.err.instruction";
  else if (instruction.length > INSTRUCTION_MAX)
    problems.instruction = "automatic.wizard.err.instruction_long";
  const steps = wholeNumber(form.maxSteps);
  if (steps === null || steps < 1 || steps > 100000)
    problems.maxSteps = "automatic.wizard.err.max_steps";
  if (form.mode === "single") {
    const episodes = wholeNumber(form.episodes);
    if (episodes === null || episodes < 1 || episodes > 1000)
      problems.episodes = "automatic.wizard.err.episodes";
  }
  return problems;
}

/** A yellow warning, not an error: a very short run cannot rate a policy. */
export function shortRunWarning(form: WizardForm): boolean {
  const steps = wholeNumber(form.maxSteps);
  return steps !== null && steps >= 1 && steps < SHORT_RUN_STEPS;
}

export function validateCampaign(form: WizardForm): Problems {
  const problems: Problems = {};
  const trials = wholeNumber(form.trialsPerArm);
  if (trials === null || trials < 2 || trials > 500)
    problems.trials = "automatic.wizard.err.trials";
  const segment = wholeNumber(form.segmentTrials);
  if (segment === null || segment < 1)
    problems.segment = "automatic.wizard.err.segment";
  else if (trials !== null && segment > trials)
    problems.segment = "automatic.wizard.err.segment_over";
  if (wholeNumber(form.seed) === null)
    problems.seed = "automatic.wizard.err.seed";
  const alpha = fractionNumber(form.alpha);
  if (alpha === null || alpha <= 0 || alpha >= 0.5)
    problems.alpha = "automatic.wizard.err.alpha";
  return problems;
}

export const hasProblems = (problems: Problems) =>
  Object.keys(problems).length > 0;

// --- requests --------------------------------------------------------------

const pad = (n: number) => String(n).padStart(2, "0");

/** A job name: a stamp plus a short readable part of the instruction. The
 * server never overwrites a job, so a taken name is a 409 and the next try
 * gets a new suffix. */
export function jobName(instruction: string, now: Date, suffix = ""): string {
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(
    now.getDate(),
  )}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const slug = instruction
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 24)
    .replace(/-+$/, "");
  return ["aeri", stamp, slug || "run", suffix].filter(Boolean).join("-");
}

/** The checkpoint a job names as its forward policy. A campaign names the
 * reference arm (else the first); each arm's own checkpoint comes in the
 * campaign request. */
export function jobForwardId(form: WizardForm): string {
  if (form.mode === "single") return form.policyId;
  return form.referenceId || form.armIds[0] || "";
}

export type JobDraft = Omit<JobCreate, "request_id" | "name">;

export function buildJobDraft(
  form: WizardForm,
  policies: PoliciesResponse | null,
): JobDraft {
  const forwardId = jobForwardId(form);
  const forward = policies?.checkpoints.find((c) => c.id === forwardId);
  const episodes =
    form.mode === "single"
      ? (wholeNumber(form.episodes) ?? 1)
      : (wholeNumber(form.trialsPerArm) ?? 1);
  const draft: JobDraft = {
    task: { instruction: form.instruction.trim() },
    policy_forward: { checkpoint_id: forwardId },
    reset: { strategy: resetStrategyOf(form), scene_check: sceneCheckOf(form) },
    run: { episodes, max_steps: wholeNumber(form.maxSteps) ?? 1 },
    termination: { allow_early_stop: form.allowEarlyStop },
    recording: forward ? { group: forward.name } : {},
  };
  if (form.resetChoice !== HUMAN_RESET)
    draft.policy_reset = { checkpoint_id: form.resetChoice };
  return draft;
}

/** What identifies "the same job": a changed form needs a new job, an
 * unchanged one reuses the job already made. */
export function jobKey(draft: JobDraft): string {
  return JSON.stringify(draft);
}

export function buildJob(
  draft: JobDraft,
  requestId: string,
  name: string,
): JobCreate {
  return { request_id: requestId, name, ...draft };
}

export function armLetter(index: number): string {
  return String.fromCharCode(65 + index);
}

export function buildArms(form: WizardForm): CampaignArmInput[] {
  return form.armIds.map((checkpoint_id, index) => ({
    id: armLetter(index),
    checkpoint_id,
    role: checkpoint_id === form.referenceId ? "reference" : "candidate",
  }));
}

export function buildCampaignRequest(
  form: WizardForm,
  jobId: string,
): CampaignPlanRequest {
  return {
    job_id: jobId,
    arms: buildArms(form),
    trials_per_arm: wholeNumber(form.trialsPerArm) ?? 0,
    schedule: {
      kind: form.scheduleKind,
      segment_trials: wholeNumber(form.segmentTrials) ?? 0,
      seed: wholeNumber(form.seed) ?? 0,
    },
    primary: {
      metric: "success",
      label_basis: form.labelBasis,
      alpha: fractionNumber(form.alpha) ?? 0.05,
    },
    preregistered: form.preregistered,
    execution_mode: form.campaignMode,
  };
}

/** The request id of one intent: the same text gives the same id until the
 * intent is done (`release`), so a repeated press is one request. */
export class IntentKeys {
  private ids = new Map<string, string>();
  idFor(intent: string): string {
    let id = this.ids.get(intent);
    if (!id) {
      id = newRequestId();
      this.ids.set(intent, id);
    }
    return id;
  }
  release(intent: string) {
    this.ids.delete(intent);
  }
}

// --- plan reading ----------------------------------------------------------

/** Whether a plan can still be launched: launchable, and its token has not
 * expired (the server refuses an expired one with 412). */
export function planUsable(
  plan: { launchable: boolean; token_expires_at: number },
  now: number,
): boolean {
  return plan.launchable && plan.token_expires_at > now;
}

/** Seconds a token has left (0 once expired). */
export function tokenSecondsLeft(expiresAt: number, now: number): number {
  return Math.max(0, Math.ceil((expiresAt - now) / 1000));
}

/** Columns and cells of the power table. The rows are whatever the planner
 * returns (design, sample size, power...); numbers are shown to 3 digits. */
export function powerTable(rows: Record<string, number | string | null>[]): {
  columns: string[];
  cells: string[][];
} {
  const columns: string[] = [];
  for (const row of rows)
    for (const key of Object.keys(row))
      if (!columns.includes(key)) columns.push(key);
  const cells = rows.map((row) =>
    columns.map((column) => {
      const value = row[column];
      if (value === null || value === undefined) return "—";
      if (typeof value === "number")
        return Number.isInteger(value)
          ? String(value)
          : String(Math.round(value * 1000) / 1000);
      return value;
    }),
  );
  return { columns, cells };
}
