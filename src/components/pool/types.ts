// Mirrors levi/pool/api.py, index.py, recipe.py, jobs.py and remote.py.

export const CATEGORIES = [
  "human",
  "rollout",
  "levi",
  "external",
  "archive",
] as const;
export type Category = (typeof CATEGORIES)[number];

export const CATEGORY_LABELS: Record<string, string> = {
  human: "Original human capture",
  rollout: "Original rollout",
  levi: "Processed by LEVI",
  external: "External",
  archive: "Archive",
};

export type OutcomeFilter =
  | "all"
  | "robot_flag_success"
  | "verified_success"
  | "human_verified_success";
export type ExportFormat = "lerobot_v21" | "recap_value" | "raw_capture";

export const FORMAT_LABELS: Record<ExportFormat, string> = {
  lerobot_v21: "LeRobot v2.1 (π0.5 / openpi)",
  recap_value: "RECAP value dataset",
  raw_capture: "Raw capture copy",
};

export interface ScanSummary {
  scanned_at: string;
  seconds: number;
  roots: string[];
  episodes: number;
  sources: number;
  tasks: number;
  nonstandard: number;
  categories: Record<string, number>;
  canonical_categories?: Record<string, number>;
  unsupported_sources?: number;
}

export interface PoolProgress {
  stages: string[];
  stage: string;
  stage_index: number;
  done: number;
  total: number;
  current: string;
  warnings: string[];
  elapsed_seconds: number;
  eta_seconds: number | null;
  bytes?: number;
  rate?: string;
  rsync_eta_seconds?: number;
}

export interface PoolJob {
  id: string;
  kind: "scan" | "export" | "push";
  status: string;
  error?: string;
  planned_at?: number;
  started_at?: number;
  finished_at?: number;
  target?: string;
  source?: string;
  destination?: string;
  dry_run?: boolean;
  remote?: RemoteTarget;
  options?: { format?: ExportFormat; name?: string };
  planned_episodes?: number;
  planned_excluded?: number;
  progress?: PoolProgress;
  result?: {
    ok?: boolean;
    dataset_path?: string;
    episodes?: number;
    frames?: number;
    excluded?: Record<string, number>;
    warnings?: string[];
    bytes?: number;
    destination?: string;
    seconds?: number;
    summary?: ScanSummary;
  };
}

export interface PoolStatus {
  enabled: boolean;
  warnings: PoolWarning[];
  roots: string[];
  export_roots: string[];
  heldout_lists: string[];
  last_scan: ScanSummary | null;
  jobs: PoolJob[];
}

export interface Facets {
  episodes: number;
  categories: Record<string, number>;
  sources: { source: string; path: string; episodes: number }[];
  formats: Record<string, number>;
  policies: Record<string, number>;
  outcomes: Record<string, number>;
  date_min: string | null;
  date_max: string | null;
  hidden_heldout: number;
  hidden_copies: number;
  archive: number;
}

export interface TaskRow {
  task: string;
  spellings: string[];
  episodes: number;
  frames: number;
  categories: Record<string, number>;
  success: number;
  failure: number;
  success_rate: number | null;
  sources: string[];
}

export interface EpisodeRow {
  key: string;
  source: string;
  source_path: string;
  format: string;
  episode: string;
  episode_index: number | null;
  task: string;
  task_raw: string | null;
  frames: number | null;
  category: string;
  outcome: string | null;
  outcome_source: string | null;
  robot_flag: string | null;
  human_label: string | null;
  policy: string | null;
  date: string | null;
  heldout: boolean;
  heldout_id: string | null;
  canonical: boolean;
  nonstandard: boolean;
  exportable: boolean;
  viewer: string | null;
}

export interface Recipe {
  name: string;
  description?: string;
  categories: string[];
  sources: string[];
  tasks: string[];
  outcome: OutcomeFilter;
  per_task_cap: number | null;
  seed: number;
  date_from: string | null;
  date_to: string | null;
  policies: string[];
  include_nonstandard: boolean;
  allow_unlinked_sources?: boolean;
  exclude: string[];
  task_text?: Record<string, string>;
  saved_at?: string;
}

export interface PoolWarning {
  code: string;
  blocking: boolean;
  message: string;
  tasks?: string[];
  episodes?: number;
  ids?: string[];
}

export interface Preview {
  warnings: PoolWarning[];
  outcome_sources: Record<string, number>;
  excluded_label_conflicts: number;
  episodes: number;
  frames: number;
  tasks: {
    task: string;
    text: string;
    episodes: number;
    frames: number;
    sources: Record<string, number>;
  }[];
  tasks_without_episodes: string[];
  categories: Record<string, number>;
  formats: Record<string, number>;
  outcomes: Record<string, number>;
  excluded: Record<string, number>;
  excluded_heldout: number;
  excluded_duplicates: number;
  excluded_nonstandard: number;
  excluded_unsupported: number;
}

export interface RemoteTarget {
  name: string;
  host: string;
  user: string | null;
  path: string;
  port: number | null;
  description?: string;
  display?: string;
}

/** Why an episode was left out (recipe.py), as UI text. */
export const REASON_LABELS: Record<string, string> = {
  heldout: "Held-out test set",
  duplicate: "Duplicate of another copy",
  nonstandard: "Non-standard folder",
  unsupported: "Format not exportable",
  not_a_raw_capture: "Not a raw capture",
  outcome_filter: "Outcome filter",
  no_outcome: "No outcome",
  per_task_cap: "Per-task cap",
  excluded_by_recipe: "Excluded by hand",
  label_conflict: "Conflicting human labels",
  conversion_preflight: "Failed capture checks",
};

/** Server warnings (recipe.find_warnings) as UI text. */
export const WARNING_LABELS: Record<string, string> = {
  heldout_unconfigured:
    "No held-out list is configured (LEVI_POOL_HELDOUT): exports are refused until it is set, or set to none.",
  heldout_lists_changed:
    "The held-out lists changed since the last scan: scan again.",
  heldout_unmatched: "Held-out entries that match no indexed episode",
  possible_unlinked_conversion:
    "Tasks taken from raw captures and from an unlinked LeRobot dataset may be the same recordings twice. Name the sources, or allow it.",
  outcome_from_robot_flag:
    "Episodes that count as verified only through the operator's key press (no human label)",
};
