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

export type CampaignPlanRequest = {
  job_id: string;
  arms: CampaignArmInput[];
  trials_per_arm: number;
  schedule: { kind: ScheduleKind; segment_trials: number; seed: number };
  primary: { metric: "success"; label_basis: string; alpha: number };
  preregistered: boolean;
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

export type CampaignTodo = {
  kind: "switch_policy" | "place_cards" | "recover_run" | null;
  // The contract leaves the rest of the todo open ("..."): these are the
  // fields the page reads when present.
  challenge?: string;
  arm_code?: string;
  cards?: string[];
  detail?: string;
};

export type CampaignArmProgress = {
  id: string;
  code: string;
  done: number;
  remaining: number;
  deviated: number;
  discarded: number;
};

export type CampaignSnapshot = {
  id: string;
  state: string;
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
