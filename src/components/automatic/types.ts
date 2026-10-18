// Types of the automatic-evaluation run API (levi2/design/aeri-ui-api-contract.md
// sections 2 and 3). Times are epoch milliseconds, ids are opaque strings.

export type ExecutionMode = "dry_run" | "shadow" | "assisted" | "autonomous";
export type ResetMode =
  | "single_reset_policy"
  | "human_assisted"
  | "subtask_loop";
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
/** What a session was asked to do (`requirements` of a plan or a snapshot,
 * read from the frozen plan). Every value may be missing or null: an older
 * server sends none of it. `success_condition` is recorded and shown only. */
export interface Requirements {
  task?: {
    instruction?: string | null;
    reset_instruction?: string | null;
    success_condition?: string | null;
  } | null;
  episodes?: number | null;
  max_steps?: { forward?: number | null; reset?: number | null } | null;
  allow_early_stop?: boolean | null;
  reset_mode?: string | null;
  policies?: {
    forward?: { checkpoint_id?: string | null; config?: string | null } | null;
    reset?: { checkpoint_id?: string | null; config?: string | null } | null;
  } | null;
  inference?: { mode?: string | null; physical_hold?: boolean | null } | null;
  /** The settings of a `subtask_loop` session (absent for the other modes
   * and from a server that does not send them). */
  subtask_loop?: SubtaskLoopSettings | null;
}

/** A declaration in the frozen plan, not the identity of the responding service. */
export interface SubtaskProvider {
  mode: "local" | "external" | "unknown";
  source: "frozen_plan" | "unavailable";
}

export interface SubtaskProgress {
  phase?: "idle" | "forward" | "reverse_pending" | "reverse" | string;
  rounds_ended?: number;
  round?: number | null;
  subtask?: {
    loop?: string | null;
    index?: number | null;
    attempt?: number | null;
    instruction?: string | null;
    episode_id?: string | null;
  } | null;
  last_check?: {
    loop?: string | null;
    index?: number | null;
    attempt?: number | null;
    value?: boolean | null;
  } | null;
}

export interface SubtaskLoopSettings {
  task?: string | null;
  rounds?: number | null;
  subtask_max_steps?: number | null;
  subtask_max_retries?: number | null;
  /** `null`: no limit ("off"). */
  max_subtasks_per_loop?: number | null;
  home_after_loop?: boolean | null;
  vlm_min_free_mem_gib?: number | null;
}

/** One `subtask_*` or `vlm_*` journal note (`GET runs/{id}/subtasks`). */
export interface SubtaskNote {
  seq: number;
  at: number | null;
  code: string;
  /** The parsed JSON detail; `null` when it was empty or did not parse. */
  detail: Record<string, unknown> | null;
  unparsed?: boolean;
  /** The whole detail's sidecar could not be used; the short one is kept. */
  sidecar_problem?: string;
}
export interface SubtaskPlanItem {
  index: number;
  pick: string | null;
  place: string | null;
  done_when: string | null;
  instruction: string | null;
}
export interface SubtaskPlanFile {
  ok: boolean;
  problem: string | null;
  subtasks: SubtaskPlanItem[];
}
export interface RequestEvidence {
  seq: number;
  visibility: "hidden" | "visible";
  context: { kind: string; round_id: string; episode_id: string | null } | null;
  report: {
    agent_id: string | null;
    agent_notes: string | null;
    tokens: Record<
      "input_new" | "output" | "cache_read" | "cache_write",
      number | null
    >;
    client_latency_ms: number | null;
    bridge_wait_ms: number | null;
    self_reported_elapsed_ms: number | null;
    prompt_text: string | null;
    raw_content: string | null;
    prompt_text_truncated: boolean;
    raw_content_truncated: boolean;
    display_text_truncated: boolean;
    prompt_bytes: number | null;
    answer_bytes: number | null;
    prompt_sha256: string | null;
    answer_content_sha256: string | null;
  } | null;
}
export interface SubtasksResponse {
  request_evidence?: RequestEvidence[];
  request_evidence_truncated?: boolean;
  reset_mode?: string | null;
  notes: SubtaskNote[];
  /** Keyed by the plan note's `sha256`. */
  plans: Record<string, SubtaskPlanFile>;
  truncated?: boolean;
}

/** The step of the episode being run: a lower bound read from the run's step
 * file, or `step: null` with the reason it is unknown. */
export interface RunProgress {
  episode_id?: string | null;
  step?: number | null;
  max_steps?: number | null;
  source?: string | null;
  lower_bound?: boolean;
  reason?: string | null;
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
  requirements?: Requirements | null;
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
  /** A standalone real-robot run: resumed only at its terminal. */
  resume_terminal_only?: boolean;
  /** A standalone real-robot run: its scene question is answered only at
   * its terminal (decision UI-2); the page shows it without an answer. */
  scene_answer_terminal_only?: boolean;
  scene_question: SceneQuestion | null;
  /** A `subtask_loop` run only (`metrics.subtask_progress`): where the loop
   * is. `episodes.done` counts rounds in this mode. */
  subtask?: SubtaskProgress | null;
  /** Absent on older servers; never assume a provider in that case. */
  subtask_provider?: SubtaskProvider | null;
  challenge: string;
  runner: { alive: boolean; pid?: number };
  requirements?: Requirements | null;
  progress?: RunProgress | null;
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

/** `GET runs/{id}/rollouts` (levi/automatic/rollout_view.py): the rollout
 * sets of a run, round -> forward / reverse set -> subtask -> attempts. A
 * `check` is the model's judgement, never a label. */
export type RolloutMedia =
  | {
      available: true;
      dataset: string;
      episode_index: number;
      viewer: string;
      videos: { camera: string; url: string }[];
    }
  | { available: false; reason: string };

export interface RolloutAttempt {
  attempt: number | null;
  episode_id: string | null;
  demo: string | null;
  instruction?: string | null;
  frames: number | null;
  seconds: number | null;
  started_at: string | null;
  ended_at: string | null;
  /** `hidden_until_labelled` while the blind rule withholds it. */
  stop_reason: string | null;
  /** `{hidden_until_labelled: true}` for a forward attempt of a round the
   * operator has not labelled yet (the blind rule). */
  check: { value?: boolean | null; hidden_until_labelled?: true } | null;
  sealed?: boolean | null;
  /** A recording that was never sealed (`incomplete_*`): no demo. */
  incomplete?: { folder: string | null; why: string | null };
  note?: string | null;
  operator_label?: string | Record<string, unknown> | null;
  media: RolloutMedia;
}

export interface RolloutSubtask {
  index: number | null;
  instruction: string | null;
  pick?: string | null;
  place?: string | null;
  attempts: RolloutAttempt[];
  attempts_truncated?: boolean;
}

export interface RolloutSet {
  plan_file: string | null;
  task?: string | null;
  outcome?: string | null;
  subtasks: RolloutSubtask[];
}

export interface Rollout {
  round: number | null;
  round_id: string | null;
  released_at: string | null;
  outcome: string | null;
  notes?: { code: string | null; detail: string | null; at: string | null }[];
  abandoned?: { phase: string | null; why: string | null };
  locked?: { reason: string | null; at: string | null };
  operator_label?: string | Record<string, unknown> | null;
  /** `null`: that side never ran in this round. */
  sets: { forward: RolloutSet | null; reverse: RolloutSet | null };
}

export interface RolloutsResponse {
  schema: string | null;
  run_id: string;
  task: string | null;
  mode: "external" | "local" | "unknown";
  reset_mode?: string | null;
  generated_from: { journal_sha256: string | null; events: number | null };
  rollouts: Rollout[];
  rollouts_truncated?: boolean;
  unmatched_demos: (string | Record<string, unknown>)[];
  problems: (string | Record<string, unknown>)[];
}
