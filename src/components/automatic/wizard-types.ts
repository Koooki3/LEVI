// Types of the automatic-evaluation (AERI) wizard and campaign pages. They
// follow levi2/design/aeri-ui-api-contract.md sections 2-4; a field the
// contract does not list is not read here. Times are epoch milliseconds, ids
// are opaque strings.

export type Bilingual = { en: string; zh: string };

// --- setup guide -----------------------------------------------------------

export type SetupLevel = 1 | 2 | 3 | 4;
export type SetupMode = "native" | "execute" | "copy";
export type SetupStatus = "ok" | "warn" | "todo" | "unknown";

export type SetupStep = {
  id: string;
  title: Bilingual;
  level: SetupLevel;
  mode: SetupMode;
  command?: string;
  why: Bilingual;
  status: SetupStatus;
  detail?: string;
};

// --- capabilities, policies, jobs ------------------------------------------

export type ExecutionMode = "dry_run" | "shadow" | "assisted" | "autonomous";

export type Capabilities = {
  adapters: {
    id: string;
    kind: "fake" | "robot";
    available: boolean;
    reason?: string;
  }[];
  modes: Record<string, { available: boolean; reason?: string }>;
  reset_modes: ResetStrategy[];
  scene_checks: SceneCheck[];
  defaults: { max_steps: number; episodes: number; reset_wait_s?: number };
};

export type ResetStrategy = "human_assisted" | "single_reset_policy";
export type SceneCheck = "provider" | "operator_attested";

export type PolicyCheckpoint = {
  id: string;
  name: string;
  role: "forward" | "reset" | "unknown";
  config: string;
  sha256_status: "verified" | "recorded" | "none";
  family?: string;
  notes: string[];
};

export type PoliciesResponse = {
  checkpoints: PolicyCheckpoint[];
  reset_available: boolean;
};

export type JobCreate = {
  request_id: string;
  name: string;
  task: { instruction: string; reset_instruction?: string };
  policy_forward: { checkpoint_id: string };
  policy_reset?: { checkpoint_id: string };
  reset: { strategy: ResetStrategy; scene_check: SceneCheck };
  run: { episodes: number; max_steps: number };
  termination?: { allow_early_stop: boolean };
  recording: { group?: string };
};

export type PlanCheck = {
  code: string;
  ok: boolean;
  severity: "info" | "warn" | "error";
  detail: string;
};

export type LaunchPlan = {
  plan_sha256: string;
  run_id: string;
  execution_mode: string;
  reset_mode: string;
  scene_check: string;
  roles: string[];
  episodes: number;
  launchable: boolean;
  refusals: string[];
  checks: PlanCheck[];
  isc: {
    id: string;
    version: string | number;
    status: "draft" | "confirmed";
  } | null;
  uses_live: { c5: boolean; gate: boolean };
  motion: boolean;
  launch_token: string;
  token_expires_at: number;
};

// --- campaigns ---------------------------------------------------------------

export type ScheduleKind =
  | "counterbalanced_segments"
  | "randomized_blocks"
  | "latin_square"
  | "interleaved"
  | "blocked";

export type CampaignArmInput = {
  id: string;
  checkpoint_id: string;
  role: "candidate" | "reference";
};

/** How a campaign is carried out (not in the contract's table; the backend
 * default is `guided`): the person's own legacy client, or a rehearsal on
 * fakes. */
export type CampaignExecutionMode = "guided" | "dry_run";

export type CampaignPlanRequest = {
  job_id: string;
  arms: CampaignArmInput[];
  trials_per_arm: number;
  schedule: { kind: ScheduleKind; segment_trials: number; seed: number };
  primary: { metric: "success"; label_basis: string; alpha: number };
  preregistered: boolean;
  execution_mode?: CampaignExecutionMode;
};

export type PowerRow = Record<string, number | string | null>;

export type CampaignPlan = {
  campaign_sha256: string;
  settings_sha256: string;
  segments: { no: number; arms: string[]; cards: string[] }[];
  switches: number;
  power: {
    detectable_difference: number | null;
    n?: number;
    rows: PowerRow[];
  };
  refusals: string[];
  checks: PlanCheck[];
  // The contract also names `plan_sha256` for the start request. The plan
  // answer is read for it when the backend supplies it.
  plan_sha256?: string;
};

/** The kinds of to-do the page knows. The server may add others: anything
 * else is shown as text with a disabled button (see `isKnownTodo`). */
export type KnownTodoKind =
  | "switch_policy"
  | "place_cards"
  | "segment_done"
  | "recover_run";

export type CampaignTodo = {
  kind: KnownTodoKind | (string & {}) | null;
  // The contract leaves the rest of the todo open ("..."); these are the
  // fields api.py puts in it, read when present.
  challenge?: string;
  segment?: number;
  arm_code?: string;
  cards?: string[];
  detail?: string;
  /** switch_policy: the checkpoint folder name and its config. */
  checkpoint?: string;
  config?: string;
  /** place_cards, segment_done (guided): the legacy client's command,
   * rendered by the server, and the note it carries. */
  command?: string;
  eval_note?: string;
  /** segment_done: what the client has written so far. */
  progress?: { done: number; planned: number };
  pending_cards?: { key: string; candidate_card: string | null }[];
  /** recover_run: why the campaign waits (a code). */
  reason?: string | null;
};

export type CampaignArmProgress = {
  id: string;
  code: string;
  done: number;
  remaining: number;
  deviated: number;
  discarded: number;
  /** Guided: episodes whose layout card nobody confirmed yet. */
  unconfirmed?: number;
};

export type CampaignSnapshot = {
  id: string;
  state: string;
  execution_mode?: CampaignExecutionMode | (string & {});
  /** Whether the campaign's controller process runs. */
  controller?: { alive: boolean };
  /** How many times the results were looked at before the end. */
  peeks?: number;
  wait_reason?: string | null;
  /** Dry run: the run of the current segment. */
  child_run_id?: string;
  updated_at?: number | null;
  arms: CampaignArmProgress[];
  segment: { no: number; total: number; arm_code: string };
  todo: CampaignTodo | null;
  safety: { faults: number; fused: boolean };
  eta_s?: number;
  blinded: boolean;
};

export type ReportFile = { name: string; kind: string };

export type CampaignReport = {
  basis: string;
  files: ReportFile[];
  analysis: Record<string, unknown>;
  manifest: Record<string, unknown>;
};

export type CommandResult = { result: string; code?: string };

/** An episode of the legacy client whose layout card waits for a person
 * (`GET .../cards`); `candidate_card` is a suggestion, never taken as given. */
export type PendingCardEpisode = {
  key: string;
  segment: number;
  run_id: string;
  number: number;
  candidate_card: string | null;
};
