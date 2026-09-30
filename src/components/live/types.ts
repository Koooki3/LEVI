// Shapes of the read-only live API (`/api/levi/live/*`, docs/LIVE.md).
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
  fault?: boolean;
  fault_reasons?: string[];
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
  spec?: string | null;
  review?: string;
  evaluated?: boolean;
  at?: number | null;
}

export interface DemoRow {
  demo: string;
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
  incomplete?: { count?: number; fr3_fault?: number; reasons?: unknown } | null;
  current?: { demos?: string[]; done?: string[] } | null;
  last_batch?: { anchored_run?: string | null; finished_at?: number } | null;
  last_processed_at?: number | null;
  last_error?: string;
}
