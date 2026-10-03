// Shapes of the live API (`/api/levi/live/*`, docs/LIVE.md): read-only except
// removing an episode and restoring it.
// Every field is optional on purpose: the page shows what the service gave it.

export type Fr3State = "ok" | "red" | "offline" | "missing";

export interface Fr3Health {
  state?: Fr3State;
  detail?: string;
  age_s?: number | null;
  robot_mode?: number | null;
  robot_mode_name?: string | null;
  current_errors?: string[];
  last_motion_errors?: string[];
  hardware_active?: boolean | null;
  controller_active?: boolean | null;
  command_success_rate?: number | null;
  reasons?: string[];
}

/** What the evaluation client itself says about the robot (C2 `fr3`). */
export interface SessionFr3 {
  ok?: boolean | null;
  robot_mode_name?: string | null;
  errors?: string[];
  hardware_active?: boolean | null;
  state_age_s?: number | null;
  source?: string | null;
  reasons?: string[];
}

export interface LiveSession {
  group: string;
  task_folder: string;
  dataset?: string;
  state: string;
  reported_state?: string;
  crashed?: boolean;
  fault?: boolean;
  age_s?: number | null;
  reason?: string;
  levi_enabled?: boolean | null;
  episode?: {
    no?: number | null;
    target?: number | null;
    counted?: number | null;
    demo_index?: number | null;
    step?: number | null;
    max_steps?: number | null;
  };
  last_episode?: {
    demo_dir?: string | null;
    outcome?: string | null;
    steps?: number | null;
    duration_s?: number | null;
    ended_at?: string | number | null;
  };
  fr3?: SessionFr3;
  prompt?: string;
  session_id?: string;
  run_id?: string;
  started_at?: number | null;
  policy?: { config?: string | null; checkpoint?: string | null };
  reset_wait_s?: number | null;
  /** Epoch seconds when it began waiting for the reset (supervisor's view). */
  waiting_reset_since?: number | null;
}

export interface DatasetRow {
  episodes: number;
  pending: number;
  annotating: number;
  done: number;
  failed: number;
  skipped?: number;
  rejected?: number;
  waiting?: number;
  backlog?: number;
  incomplete?: number;
  fr3_fault?: number;
  discarded?: number;
  available?: boolean;
  last_processed_at?: number | null;
  last_error?: string;
  state?: string;
  /** What a person must do when state is awaiting_approval. */
  awaiting?: "plan" | "changes" | null;
  review_runs_open?: number;
  stuck?: number;
  source_changed?: number;
  fault?: boolean;
  fault_reasons?: string[];
  /** Episodes a person removed (restorable); not counted above. */
  excluded?: number;
}

export interface VllmInfo {
  state?: string;
  port?: number;
  profile?: string | null;
  max_model_len?: number | null;
  started_at?: number | null;
  error?: string;
  owned?: boolean;
}

export interface ServiceStatus {
  schema?: string;
  pid?: number;
  started_at?: number;
  updated_at?: number;
  state?: string;
  accepts_sessions?: boolean;
  ui_url?: string;
  core_port?: number;
  auto_approve?: boolean;
  watch_roots?: string[];
  gpu?: {
    mode?: string;
    configured_mode?: string;
    vllm_state?: string;
    vllm?: VllmInfo;
    policy_server_seen?: boolean;
    gate?: { open?: boolean; code?: string; reason?: string };
    decision?: { allowed?: boolean; code?: string; reason?: string };
    free_mib?: number | null;
    lock_held?: boolean;
  };
  datasets?: Record<string, DatasetRow>;
  queue_depth?: number;
  worker?: {
    pid?: number;
    dataset?: string | null;
    phase?: string | null;
    note?: string | null;
    started_at?: number | null;
  } | null;
  sessions?: LiveSession[];
  fr3?: Fr3Health;
  /** Set when the service needs a person (`levi live resume`). */
  /** Why nothing is being labelled, when that does not pass by itself. */
  labelling_paused?: { code?: string; reason?: string; since?: number } | null;
  /** When the main loop last ticked (updated_at comes from another thread). */
  loop_at?: number | null;
  attention?: { code?: string; reason?: string; since?: number } | null;
  /** Whether the page / core API the service starts came up. */
  frontend?: {
    state?: string;
    error?: string;
    attempts?: number;
    ui?: boolean;
    core_port?: number;
  } | null;
  events?: { time: number; level?: string; text: string }[];
  last_error?: string;
  resources?: {
    rss_mb?: number | null;
    threads?: number | null;
    cpu_percent?: number | null;
  };
}

export interface LiveStatusResponse {
  enabled: boolean;
  alive?: boolean;
  age_s?: number | null;
  service?: ServiceStatus | null;
  faults?: { dataset: string; reasons: string[] }[];
  fr3_red?: boolean;
  /** Runs the live gate stopped that a person started: `waiting` continue by
   * themselves, `needs_person` do not (docs/LIVE.md). */
  blocked_runs?: {
    count?: number;
    waiting?: string[];
    needs_person?: string[];
  };
}

export interface LiveSessionsResponse {
  enabled: boolean;
  sessions?: LiveSession[];
  fr3?: Fr3Health;
  active?: boolean;
}

export interface Verdict {
  outcome?: string | null;
  events?: number | null;
  valid_events?: number | null;
  undecided?: boolean | null;
  /** Set only under a rule beyond "any valid release" (`last_valid_not_regrasped`). */
  rule?: string | null;
  /** Last `place` time segment's outcome: success, failure, unknown, none (no
   * such segment) or missing (no time segments to read). */
  place_outcome?: string | null;
  /** Times the gripper closed again after the last valid release. */
  closes_after_last_valid?: number | null;
  spec?: string | null;
  review?: string;
  evaluated?: boolean;
  at?: number | null;
}

/** Who removed an episode, when and why (`excluded` of a dataset's demo). */
export interface Removal {
  at?: number | null;
  by?: string;
  reason?: string;
}

export interface DemoRow {
  demo: string;
  /** Set when a person removed the episode (it is in `excluded_demos`). */
  excluded?: Removal | null;
  state?: string;
  episode_index?: number | null;
  run_id?: string | null;
  completed_at?: number | null;
  attempts?: number;
  reason?: string | null;
  segments?: number | null;
  committed_at?: number | null;
  verdict?: (Verdict & { run_id?: string | null }) | null;
}

export interface DatasetDetail {
  enabled: boolean;
  name: string;
  repo_id?: string | null;
  group?: string | null;
  task_folder?: string | null;
  task_text?: string | null;
  counts?: Record<string, number>;
  total_demos?: number;
  demos?: DemoRow[];
  /** Removed episodes, newest first (kept apart from `demos`). */
  excluded_count?: number;
  excluded_demos?: DemoRow[];
  incomplete?: { count?: number; fr3_fault?: number; reasons?: unknown } | null;
  current?: { demos?: string[]; done?: string[] } | null;
  /** Ids of the review runs still open for a person. */
  review_runs?: string[];
  last_batch?: { anchored_run?: string | null; finished_at?: number } | null;
  last_processed_at?: number | null;
  last_error?: string;
}

/** The answer of removing or restoring episodes. */
export interface ChangeResult {
  enabled?: boolean;
  dataset: string;
  changed: string[];
  unchanged: string[];
  counts?: Record<string, number>;
  excluded_count?: number;
  review_runs_open?: number;
  /** Open review runs all of whose episodes are removed: still open, only
   * left out of the count. */
  review_hidden?: string[];
}
