// Types of the automatic-evaluation run API (levi2/design/aeri-ui-api-contract.md
// sections 2 and 3). Times are epoch milliseconds, ids are opaque strings.

export type ExecutionMode = "dry_run" | "shadow" | "assisted" | "autonomous";
export type ResetMode = "single_reset_policy" | "human_assisted";
export type SceneCheck = "provider" | "operator_attested";
export type OperatorLabel = "success" | "failure" | "discarded" | "unclear";

export interface JobEntry {
  id: string;
  name: string;
  modified?: number;
  valid: boolean;
  reset_mode?: ResetMode | null;
  errors?: string[];
}
export interface JobsResponse {
  roots?: string[];
  jobs: JobEntry[];
}

export interface PlanCheck {
  code: string;
  ok: boolean;
  severity: "info" | "warn" | "error";
  detail?: string;
}
export interface LaunchPlan {
  plan_sha256: string;
  run_id: string;
  execution_mode: ExecutionMode;
  reset_mode: ResetMode;
  scene_check?: SceneCheck | null;
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
  uses_live?: { c5: boolean; gate: boolean };
  motion?: boolean;
  launch_token: string;
  token_expires_at: number;
}

export interface RunSummary {
  run_id: string;
  state: string;
  reset_mode: ResetMode;
  execution_mode: ExecutionMode;
  episodes_done: number;
  episodes_total: number;
  started_at?: number;
  updated_at?: number;
}

export interface PendingCard {
  reason: string;
  resume_seq: number;
  waited_ms: number;
  nth_wait: number;
  last_episode: {
    episode_id: string;
    rollout_label?: string | null;
    ended_by?: string | null;
    stop_reason?: string | null;
  } | null;
  contract: { id_version: string; status: string; predicates: string[] };
  assessment: {
    decision: string;
    failed: string[];
    unknown: string[];
    frame_refs: string[];
  } | null;
  operator_label: {
    current: OperatorLabel | null;
    automatic_verdict: string | null;
    hidden_until_labelled: boolean;
  };
}

export interface ScenePredicate {
  name: string;
  text: string;
  required: boolean;
}
export interface SceneQuestion {
  request_id: string;
  nonce: string;
  frames_sha256: string;
  frames: string[];
  predicates: ScenePredicate[];
  asked_at: number;
  timeout_s: number;
}

export interface RunSnapshot {
  run_id: string;
  state: string;
  seq: number;
  reset_mode: ResetMode;
  scene_check?: SceneCheck | null;
  execution_mode: ExecutionMode;
  episodes: {
    done: number;
    total: number;
    /** The episode the run is on: its id (what the service sends), or an
     * object with its number and step progress. */
    current?:
      | string
      | {
          episode_id?: string;
          no?: number;
          step?: number;
          max_steps?: number;
        }
      | null;
  };
  counters: {
    planned_interventions: number;
    unplanned_interventions: number;
    faults: number;
  };
  pending_card: PendingCard | null;
  scene_question: SceneQuestion | null;
  challenge: string;
  runner: { alive: boolean; pid?: number };
  updated_at?: number;
}

export interface RunEvent {
  seq: number;
  at: number;
  kind: string;
  from?: string | null;
  to?: string | null;
  reason?: string | null;
}
export interface EventsResponse {
  events: RunEvent[];
  next: number;
}

/** `metrics.report` (levi/automatic/metrics.py): a nested JSON of groups. */
export type MetricsReport = Record<string, unknown> & {
  reset_mode?: ResetMode | null;
  scene_check?: SceneCheck | null;
  comparable?: string[];
  mode_specific?: string[];
};

export interface CommandResult {
  result: "applied" | "repeated" | "refused";
  code?: string;
}
export interface LabelResult {
  label: OperatorLabel;
  revealed: boolean;
  card: PendingCard | null;
}
