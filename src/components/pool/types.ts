// Mirrors levi/pool/api.py, index.py, recipe.py, jobs.py and remote.py.
import { serverSentence } from "@/components/pages-ui/messages";

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

/** How a rollout was run (levi/pool/policy.py), as UI text. */
export const POLICY_METHODS = [
  "direct",
  "dsrl",
  "rlt",
  "sfe",
  "student",
  "other",
  "unknown",
] as const;
export type PolicyMethod = (typeof POLICY_METHODS)[number];

export const METHOD_LABELS: Record<string, string> = {
  direct: "Direct deployment",
  dsrl: "DSRL online RL",
  rlt: "RLT online RL",
  sfe: "SFE online RL",
  student: "Student policy",
  other: "Other method",
  unknown: "Unknown method",
};

/** Gripper classes the index records (levi/pool/embodiment.py); anything else
 * a workspace's rules add is shown as recorded. */
export const GRIPPER_LABELS: Record<string, string> = {
  robotiq_2f85: "Robotiq 2F-85",
  franka_hand: "Franka Hand",
  unknown: "Unknown gripper",
};

export function gripperLabel(
  gripper: string | null | undefined,
  t: (key: string) => string,
): string {
  const value = gripper || "unknown";
  return t(GRIPPER_LABELS[value] || value);
}

/** ``Robotiq 2F-85 120 · Franka Hand 30`` (largest first, unknown last on a tie). */
export function gripperCounts(
  counts: Record<string, number>,
  t: (key: string) => string,
): string {
  return Object.entries(counts)
    .sort(
      (a, b) =>
        b[1] - a[1] ||
        Number(a[0] === "unknown") - Number(b[0] === "unknown") ||
        a[0].localeCompare(b[0]),
    )
    .map(([g, n]) => `${gripperLabel(g, t)} ${n.toLocaleString("en-US")}`)
    .join(" · ");
}

/** Tooltip of the gripper cell: the four recorded fields and what decided
 * the gripper (index columns, not translated). */
export function gripperTitle(
  row: Pick<
    EpisodeRow,
    "robot" | "gripper" | "action_mode" | "ee_frame" | "embodiment_evidence"
  >,
  t: (key: string) => string,
): string | undefined {
  if (row.gripper === undefined && row.robot === undefined) return undefined;
  const why = row.embodiment_evidence?.gripper;
  return [
    `${t("Gripper")}: ${row.gripper || "unknown"}${why ? ` (${why})` : ""}`,
    `${t("Robot")}: ${row.robot || "unknown"}`,
    `${t("Action mode")}: ${row.action_mode || "unknown"}`,
    `${t("End-effector frame")}: ${row.ee_frame || "unknown"}`,
  ].join("\n");
}

/** The gripper was declared by a person for its source, not read from the
 * episode's metadata. */
export function gripperDeclared(
  row: Pick<EpisodeRow, "embodiment_evidence">,
): boolean {
  // "declared: <source>", or taken from a linked capture that was declared;
  // "declared agrees" and conflicts are not declarations the value rests on.
  return /^(linked capture: )?declared: /.test(
    row.embodiment_evidence?.gripper ?? "",
  );
}

/** ``pi05_fr3_all_step49999`` -> ``step49999`` (as the server's label). */
export function shortCheckpoint(checkpoint: string | null): string | null {
  if (!checkpoint) return null;
  const match = /(step[_-]?\d+)$/.exec(checkpoint);
  return match ? match[1] : checkpoint;
}

/** ``pi05_fr3_all_state · step49999 · DSRL online RL`` in the page language. */
export function policyLabel(
  row: Pick<EpisodeRow, "policy_model" | "policy_checkpoint" | "policy_method">,
  t: (key: string) => string,
): string | null {
  if (!row.policy_method && !row.policy_model) return null;
  return [
    row.policy_model,
    shortCheckpoint(row.policy_checkpoint),
    row.policy_method
      ? t(METHOD_LABELS[row.policy_method] || row.policy_method)
      : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export type OutcomeFilter =
  | "all"
  | "robot_flag_success"
  | "verified_success"
  | "checked_success"
  | "human_verified_success";
/** Which held-back episodes the episode list shows (all of them by default). */
export type HoldbackFilter = "all" | "only" | "hide";
export type ExportFormat = "lerobot_v21" | "recap_value" | "raw_capture";

export const FORMAT_LABELS: Record<ExportFormat, string> = {
  lerobot_v21: "LeRobot v2.1 (π0.5 / openpi)",
  recap_value: "RECAP value dataset",
  raw_capture: "Raw capture copy",
};

/** How the export's frames meet its fps (levi/pool/timing.py). */
export type Timing = "resample" | "retime";

/** The format's own timing; a raw capture copy has none. */
export function defaultTiming(format: ExportFormat): Timing | null {
  if (format === "raw_capture") return null;
  return format === "recap_value" ? "retime" : "resample";
}

export const TIMING_LABELS: Record<Timing, string> = {
  resample: "Resample (drop frames only)",
  retime: "Retime (keep every frame)",
};

export const TIMING_HINTS: Record<Timing, string> = {
  resample:
    "Drops frames only, never adds any (refused when a source's frame rate is below the export FPS).",
  retime:
    "Keeps every frame and declares it at the export FPS (the time axis stretches or shrinks slightly, so motion plays slightly faster or slower).",
};

/** Reset data: forward demonstrations reversed in time (levi/pool/reset/). */
export type ResetDirection =
  | "forward_only"
  | "forward_and_reset"
  | "reset_only";
export type ResetMaxRelease = "in_place" | "in_reach";
export type ResetOnIneligible = "exclude" | "partial";
export type ReleaseClass = "in_place" | "in_reach" | "escaped" | "unknown";

export const RESET_DIRECTIONS: ResetDirection[] = [
  "forward_only",
  "forward_and_reset",
  "reset_only",
];

/** ``options.reset`` of ``POST pool/export`` (levi/pool/reset/schema.py). */
export interface ResetOptions {
  direction: ResetDirection;
  task_template: string;
  action_contract: string;
  max_release: ResetMaxRelease;
  on_ineligible: ResetOnIneligible;
  require_forward_success?: boolean;
  release_camera?: string;
  gripper_lead_rows?: number;
  allow_no_grasp?: boolean;
  min_settled_rows?: number;
  review_model?: string | null;
  bridges?: { source: string; record: string }[];
}

/** One release (the gripper opening on an object) in a reversed episode. */
export interface ResetRelease {
  row: number;
  hold_row?: number | null;
  rest_row?: number | null;
  class: ReleaseClass;
  reason: string | null;
  metrics?: Record<string, unknown>;
  seam?: { position_jump: number; rotation_jump: number };
}

export interface ResetEpisodeVerdict {
  key: string;
  task?: string | null;
  source?: string | null;
  eligible: boolean;
  reason: string | null;
  detail?: string;
  scope?: "full" | "partial";
  generation?: string;
  rows?: number;
  rows_kept?: number;
  releases: ResetRelease[];
}

/** ``POST pool/reset/analyze``. */
export interface ResetAnalysis {
  summary: {
    episodes: number;
    reversible: number;
    reasons: Record<string, number>;
    release_classes: Partial<Record<ReleaseClass, number>>;
  };
  episodes: ResetEpisodeVerdict[];
  selected: number;
  analyzed: number;
  profile: string;
}

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
  updated_at?: number;
}

/** Job states (levi/pool/jobs.py). */
export type JobStatus =
  | "planned"
  | "running"
  | "stalled"
  | "cancelling"
  | "cancelled"
  | "interrupted"
  | "failed"
  | "done"
  | "done_with_warnings"
  | "done_with_errors";

/** A fatal error made readable (levi/pool/joblog.py ``describe_*``). */
export interface JobErrorInfo {
  type: string;
  message: string;
  stage?: string;
  hint?: string;
}

export interface PoolJob {
  id: string;
  kind: "scan" | "export" | "push";
  status: JobStatus | string;
  error?: string;
  reason?: string;
  error_info?: JobErrorInfo | null;
  planned_at?: number;
  started_at?: number;
  finished_at?: number;
  interrupted_at?: number;
  resumes?: number;
  /** Seconds since the worker's last heartbeat or progress write. */
  age_seconds?: number | null;
  /** Seconds since its work last moved. */
  idle_seconds?: number | null;
  updated_at?: number | null;
  resumable?: boolean;
  rerunnable?: boolean;
  partial?: string | null;
  failures?: number;
  /** Episodes left out by a data check / failed with an exception (errors.jsonl). */
  left_out?: number;
  failed?: number;
  warning_count?: number;
  log_bytes?: number;
  target?: string;
  source?: string;
  destination?: string;
  dry_run?: boolean;
  remote?: RemoteTarget;
  options?: {
    format?: ExportFormat;
    name?: string;
    reset?: ResetOptions;
  };
  planned_episodes?: number;
  /** Reset export: recorded stretches (bridges) the plan would use. */
  planned_bridge_records?: number;
  planned_excluded?: number;
  progress?: PoolProgress;
  result?: {
    ok?: boolean;
    dataset_path?: string;
    episodes?: number;
    frames?: number;
    excluded?: Record<string, number>;
    /** Reset episodes written (levi/pool/reset/). */
    reset_episodes?: number;
    warnings?: (string | { code: string; message: string })[];
    errors?: number;
    /** Left out by a data check / failed with an exception (errors = both). */
    left_out?: number;
    failed?: number;
    resumed?: boolean;
    bytes?: number;
    destination?: string;
    seconds?: number;
    summary?: ScanSummary;
  };
}

/** One warning of an export (levi/pool/jobs.py ``details``). */
export interface JobWarning {
  code?: string;
  message?: string;
  level?: string;
  blocking?: boolean;
  count?: number;
  episodes?: string[];
  episodes_total?: number;
}

/** ``GET pool/jobs/{id}/details``: the outcome, warnings, left-out episodes
 * (by reason) and failures of one job. */
export interface JobDetails {
  id: string;
  kind: string;
  status: string;
  summary: {
    episodes?: number;
    frames?: number;
    bytes?: number;
    excluded?: Record<string, number>;
    reset_episodes?: number;
  };
  warnings: JobWarning[];
  left_out: {
    total: number;
    groups: { code: string; count: number }[];
    items: {
      episode: string;
      stage?: string;
      message: string;
      reasons: string[];
    }[];
  };
  failed: {
    total: number;
    items: {
      episode: string;
      stage?: string;
      type?: string;
      message: string;
      traceback: string;
    }[];
  };
  error?: string | null;
  error_info?: JobErrorInfo | null;
}

export interface PoolDisk {
  path: string;
  free_bytes: number;
  total_bytes: number;
}

export interface PoolStatus {
  enabled: boolean;
  warnings: PoolWarning[];
  roots: string[];
  export_roots: string[];
  heldout_lists: string[];
  /** LEVI_POOL_HOLDBACK and LEVI_POOL_OUTCOMES (absent from an older server). */
  holdback_lists?: string[];
  outcome_lists?: string[];
  last_scan: ScanSummary | null;
  disk?: PoolDisk[];
  jobs: PoolJob[];
}

export interface Facets {
  episodes: number;
  categories: Record<string, number>;
  sources: { source: string; path: string; episodes: number }[];
  formats: Record<string, number>;
  policies: Record<string, number>;
  policy_models: Record<string, number>;
  policy_checkpoints: Record<string, number>;
  policy_methods: Record<string, number>;
  robots?: Record<string, number>;
  grippers?: Record<string, number>;
  outcomes: Record<string, number>;
  date_min: string | null;
  date_max: string | null;
  hidden_heldout: number;
  /** Episodes on a hold-back list (listed; left out of recipes by default). */
  holdback?: number;
  /** Hold-back entries that match no indexed episode (they hold nothing back). */
  holdback_unmatched?: number;
  /** Episodes removed on the live page (never listed or exported). */
  removed_in_live?: number;
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
  policy_methods?: Record<string, number>;
  grippers?: Record<string, number>;
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
  policy_model: string | null;
  policy_checkpoint: string | null;
  policy_method: string | null;
  policy_phase: string | null;
  policy_label: string | null;
  robot?: string | null;
  gripper?: string | null;
  action_mode?: string | null;
  ee_frame?: string | null;
  embodiment_evidence?: Record<string, string>;
  date: string | null;
  heldout: boolean;
  heldout_id: string | null;
  /** On a hold-back list: set aside for now (a recipe leaves it out unless
   * it includes held-back episodes). */
  holdback?: boolean;
  holdback_set?: string | null;
  /** The check that ranks below a human label and above the robot flag. */
  verified_outcome?: string | null;
  verified_by?: string | null;
  canonical: boolean;
  nonstandard: boolean;
  exportable: boolean;
  viewer: string | null;
}

export type Strategy = "quality" | "random" | "first";
export const STRATEGIES: Strategy[] = ["quality", "random", "first"];

/** One task of a recipe (levi/pool/recipe.py TaskEntry): how many episodes it
 * contributes (null = the recipe's per-task cap, else all), the target share of
 * successes (null = the task's own share) and how they are picked. */
export interface TaskEntry {
  task: string;
  count: number | null;
  success_ratio: number | null;
  strategy: Strategy;
}

export interface Recipe {
  name: string;
  description?: string;
  categories: string[];
  sources: string[];
  tasks: TaskEntry[];
  outcome: OutcomeFilter;
  per_task_cap: number | null;
  seed: number;
  date_from: string | null;
  date_to: string | null;
  policies: string[];
  policy_models?: string[];
  policy_checkpoints?: string[];
  policy_methods?: string[];
  robots?: string[];
  grippers?: string[];
  allow_mixed_gripper?: boolean;
  include_nonstandard: boolean;
  /** Also take episodes on a hold-back list (left out otherwise). */
  include_holdback?: boolean;
  allow_unlinked_sources?: boolean;
  exclude: string[];
  task_text?: Record<string, string>;
  // Versions of reviewed task text corrections to apply (docs/TRAINING_POOL.md).
  task_corrections?: string[];
  saved_at?: string;
}

export interface PoolWarning {
  code: string;
  blocking: boolean;
  message: string;
  tasks?: string[];
  episodes?: number | string[];
  ids?: string[];
  /** copy_task_conflict: how many picked episodes; ``episodes`` is then the
   * list of their keys (up to 50), not a count. */
  count?: number;
  /** The export would certainly be refused (the plan still runs). */
  refused?: boolean;
  /** "info" notes inform; they never block. */
  level?: "info";
  /** mixed_gripper: why (mixed_known, known_and_unknown), whether the recipe
   * allowed it, and the count per gripper class. */
  problem?: "mixed_known" | "known_and_unknown" | "unknown_multi_source" | null;
  allowed?: boolean;
  counts?: Record<string, number>;
  /** Episodes with no recorded gripper, per source. */
  unknown_sources?: Record<string, number>;
  export_fps?: number;
  min_source_fps?: number;
  suggested_fps?: number;
  max_deviation_percent?: number;
  direction?: "shorter" | "longer";
}

/** Whether a warning stops the export (server-blocking, or certainly
 * refused at this fps and timing). */
export function stopsExport(w: PoolWarning): boolean {
  return !!w.blocking || !!w.refused;
}

export const TIMING_WARNING_CODES = new Set([
  "source_fps_below_export",
  "retime_time_scale",
]);

const TIMING_WARNING_TEXT: Record<string, (w: PoolWarning) => string> = {
  source_fps_below_export: () =>
    "{count} raw episode(s) were recorded below the export FPS {fps} (as slow as {min} FPS). Resample cannot add frames, so the export would be refused. Lower the FPS to {suggested}, or switch the timing to Retime.",
  retime_time_scale: (w) =>
    w.direction === "longer"
      ? "Retime: the time axis of {count} raw episode(s) becomes up to {percent}% longer than recorded (motion plays that much slower)."
      : "Retime: the time axis of {count} raw episode(s) becomes up to {percent}% shorter than recorded (motion plays that much faster).",
};

const GRIPPER_TEXT = {
  mixed_known:
    "The selection mixes grippers: {counts}. An export takes one gripper: filter by gripper, or allow mixing.",
  known_and_unknown:
    "The selection mixes episodes of a known gripper with episodes whose gripper is not recorded: {counts}. Filter by gripper, list Unknown gripper on purpose, or allow mixing.",
  unknown_multi_source:
    "The selection takes episodes with no recorded gripper from several sources: {sources}. A source with no gripper record may hold either gripper. Declare each source's gripper in pool/rules.json, list Unknown gripper on purpose, or allow mixing.",
  allowed: "Grippers are mixed on purpose: {counts}.",
  unknown:
    "{count} episode(s) from {sources} have no recorded gripper (unknown): their metadata does not say which gripper was used.",
};

/** ``legacy 2 · legacy2 2`` (the first five sources, then how many more). */
export function sourceCounts(counts: Record<string, number>): string {
  const entries = Object.entries(counts).sort(
    (a, b) => b[1] - a[1] || a[0].localeCompare(b[0]),
  );
  const shown = entries
    .slice(0, 5)
    .map(([s, n]) => `${s} ${n.toLocaleString("en-US")}`)
    .join(" · ");
  return entries.length > 5 ? `${shown} · +${entries.length - 5}` : shown;
}

/** A warning as UI text; the timing and gripper notes carry their numbers. */
export function warningText(
  w: PoolWarning,
  t: (key: string) => string,
  language: "en" | "zh" = "en",
): string {
  const episodes = typeof w.episodes === "number" ? w.episodes : (w.count ?? 0);
  if (w.code === "mixed_gripper") {
    const key = w.allowed
      ? GRIPPER_TEXT.allowed
      : GRIPPER_TEXT[w.problem || "mixed_known"];
    return t(key)
      .replace("{counts}", gripperCounts(w.counts || {}, t))
      .replace("{sources}", sourceCounts(w.unknown_sources || {}));
  }
  if (w.code === "gripper_unknown") {
    return t(GRIPPER_TEXT.unknown)
      .replace("{count}", String(episodes))
      .replace("{sources}", sourceCounts(w.unknown_sources || {}));
  }
  const template = TIMING_WARNING_TEXT[w.code];
  if (!template)
    return WARNING_LABELS[w.code]
      ? t(WARNING_LABELS[w.code])
      : serverSentence(w.message, t, language);
  return t(template(w))
    .replace("{count}", String(episodes))
    .replace("{min}", String(w.min_source_fps ?? ""))
    .replace("{fps}", String(w.export_fps ?? ""))
    .replace("{suggested}", String(w.suggested_fps ?? ""))
    .replace("{percent}", String(w.max_deviation_percent ?? ""));
}

/** Per task, what selection did (levi/pool/select.py ``choose``). */
export interface TaskReport {
  task: string;
  strategy: Strategy;
  requested: number | null;
  success_ratio: number | null;
  available: number;
  successes: number;
  failures: number;
  unknown: number;
  already_used: number;
  selected: number;
  selected_successes: number;
  selected_failures: number;
  selected_unknown: number;
  shortfall: number;
  shortfall_successes: number;
  shortfall_failures: number;
  notes: string[];
  note: string;
}

export interface Mix {
  episodes: number;
  successes: number;
  failures: number;
  unknown: number;
  success_share: number | null;
  categories: Record<string, number>;
  policy_methods: Record<string, number>;
  lean: { dimension: string; value: string; share: number } | null;
}

/** ``POST pool/suggest``: what adding a task offers. */
export interface Suggest {
  task: string;
  available: number;
  successes: number;
  failures: number;
  unknown: number;
  already_used: number;
  suggested_count: number;
  earlier_counts: number[];
}

export interface PickedEpisode {
  key: string;
  source: string;
  source_path: string;
  format: string;
  episode: string;
  episode_index: number | null;
  frames: number | null;
  outcome: string | null;
  outcome_source: string | null;
  holdback?: boolean;
  policy_label: string | null;
  date: string | null;
  quality_score: number;
  sel_stratum: string;
  selection_reason: string[];
  viewer: string | null;
}

export interface PickedEpisodes {
  task: string;
  report: TaskReport;
  episodes: PickedEpisode[];
}

export interface Preview {
  warnings: PoolWarning[];
  mix?: Mix;
  outcome_sources: Record<string, number>;
  excluded_label_conflicts: number;
  episodes: number;
  frames: number;
  tasks: (TaskReport & {
    text: string;
    episodes: number;
    frames: number;
    sources: Record<string, number>;
  })[];
  tasks_without_episodes: string[];
  categories: Record<string, number>;
  formats: Record<string, number>;
  outcomes: Record<string, number>;
  policy_methods?: Record<string, number>;
  grippers?: Record<string, number>;
  robots?: Record<string, number>;
  /** Episodes whose robot / gripper / frame comes from a declaration, per field. */
  declared?: Record<string, number>;
  excluded: Record<string, number>;
  excluded_heldout: number;
  /** Left out because they are on a hold-back list. */
  excluded_holdback?: number;
  holdback?: {
    lists: string[];
    include: boolean;
    left_out: number;
    included: number;
  };
  excluded_in_live?: number;
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
  held_back: "Held back (set aside for now)",
  duplicate: "Duplicate of another copy",
  nonstandard: "Non-standard folder",
  unsupported: "Format not exportable",
  not_a_raw_capture: "Not a raw capture",
  outcome_filter: "Outcome filter",
  no_outcome: "No outcome",
  per_task_cap: "Per-task cap",
  not_selected: "Not picked (over the task's count)",
  already_in_composition: "Already picked for an earlier task",
  excluded_by_recipe: "Excluded by hand",
  excluded_in_live: "Removed on the live page",
  label_conflict: "Conflicting human labels",
  conversion_preflight: "Failed capture checks",
  convert_error: "Conversion failed",
  reset_forward_failed: "Reset data: the demonstration did not succeed",
  reset_forward_unlabeled: "Reset data: the demonstration has no outcome label",
  reset_already_reset: "Reset data: already a reset episode",
  reset_action_contract:
    "Reset data: the actions do not follow the action contract",
  reset_release_escaped:
    "Reset data: the object ended out of the gripper's reach after a release",
  reset_release_unknown:
    "Reset data: a release could not be judged from the images",
  reset_release_in_reach:
    "Reset data: the object settled within the fingers' reach after a release",
  reset_unreadable: "Reset data: the episode could not be read",
  reset_bridge_missing: "Reset data: the recorded stretch is not in the pool",
  reset_bridge_contract:
    "Reset data: the recorded stretch does not follow the action contract",
  reset_bridge_start_mismatch:
    "Reset data: the recorded stretch does not start where the demonstration ended",
  reset_bridge_no_grasp:
    "Reset data: the recorded stretch never grasps the object",
  reset_bridge_never_reaches_anchor:
    "Reset data: the recorded stretch never reaches the last safe hold",
  reset_bridge_nothing_held:
    "Reset data: nothing is held at the end of the recorded stretch",
  reset_bridge_visual_mismatch:
    "Reset data: the recorded stretch shows a different scene",
  reset_bridge_visual_unchecked:
    "Reset data: the recorded stretch could not be compared with the demonstration",
  reset_no_grasp:
    "Reset data: the episode has no grasp (pushing, pouring or wiping is not reversed)",
  reset_video_rows:
    "Reset data: the video and the state rows have different lengths",
  reset_bridge_cameras:
    "Reset data: the recorded stretch lacks a camera or has another resolution",
  reset_write_error: "Reset data: writing this reset episode failed",
};

/** Server warnings (recipe.find_warnings) as UI text. */
export const WARNING_LABELS: Record<string, string> = {
  heldout_unconfigured:
    "No held-out list is configured (LEVI_POOL_HELDOUT): exports are refused until it is set, or set to none.",
  heldout_lists_changed:
    "The held-out lists changed since the last scan: scan again.",
  heldout_unmatched: "Held-out entries that match no indexed episode",
  holdback_lists_changed:
    "The hold-back lists changed since the last scan: scan again.",
  holdback_list_unreadable:
    "A hold-back list (LEVI_POOL_HOLDBACK) cannot be read: exports are refused until it can.",
  holdback_unmatched: "Hold-back entries that match no indexed episode",
  holdback_included:
    "Held-back episodes are in this selection (the recipe includes them)",
  outcomes_lists_changed:
    "The verified-outcome lists changed since the last scan: scan again.",
  outcomes_list_unreadable:
    "A verified-outcome list (LEVI_POOL_OUTCOMES) cannot be read: exports are refused until it can.",
  outcomes_unmatched: "Verified-outcome entries that match no indexed episode",
  outcomes_conflict:
    "Recordings whose verified outcome disagrees with a human label (the label is used) or with another verified entry (ignored)",
  possible_unlinked_conversion:
    "Tasks taken from raw captures and from an unlinked LeRobot dataset may be the same recordings twice. Name the sources, or allow it.",
  outcome_from_robot_flag:
    "Episodes that count as verified only through the operator's key press (no human label)",
};

/** Where an episode's outcome came from (index ``outcome_source``). */
export const OUTCOME_SOURCE_LABELS: Record<string, string> = {
  human: "human label",
  verified: "verified",
  robot_flag: "robot flag",
  sft_demonstration: "demonstration",
  none: "none",
};

/** Why the selection took an episode (select.py ``score_rows``), as UI text. */
export const PICK_REASONS: Record<string, string> = {
  human: "human label",
  verified: "verified outcome",
  robot_flag: "robot flag",
  sft_demonstration: "demonstration",
  no_outcome: "no outcome",
  conflicting_labels: "conflicting labels",
  efficient: "efficient length",
  slow: "slower than most successes",
  very_short: "shorter than most successes",
  full_attempt: "full attempt",
  short_attempt: "short attempt",
  odd_length: "unusual length",
  unscored: "not scored by outcome",
};

/** Selection notes (select.py ``MESSAGES``), as UI text. */
export const SELECTION_NOTES: Record<string, string> = {
  fewer_available: "Fewer episodes are available than requested",
  success_short:
    "Not enough successes for the requested share; failures fill in",
  failure_short:
    "Not enough failures for the requested share; successes fill in",
  unknown_outcomes_used:
    "Episodes with no reliable outcome were needed to reach the count",
  no_outcomes:
    "No episode of this task has a recorded outcome; the success share is not applied",
  already_used: "Episodes already picked for an earlier task were skipped",
};

export const STRATEGY_LABELS: Record<Strategy, string> = {
  quality: "Smart pick",
  random: "Random",
  first: "In order",
};

/** ``GET pool/jobs/{id}/delete-preview``: what clearing or deleting a job
 * would remove (levi/pool/deletion.py). */
export interface DeleteOutput {
  role: "output" | "partial";
  path: string;
  exists: boolean;
  bytes?: number;
  episodes?: number | null;
  format?: string | null;
  created_at?: string | null;
  owner: string | null;
  owned: boolean;
  will_delete: boolean;
  kept_because?: string | null;
  needs_force: string[];
  refusals: string[];
  shared_with: string[];
  pushed: { job: string; remote: string | null; status: string; at?: number }[];
}

export interface DeletePlan {
  id: string;
  kind: string;
  status: string;
  running: boolean;
  record_files: number;
  record_bytes: number;
  outputs: DeleteOutput[];
  refused: string | null;
  needs_force: boolean;
  freed_bytes: number;
}

export interface DeleteResult {
  id: string;
  record_cleared: boolean;
  removed: { role: string; path: string; bytes: number; files: number }[];
  kept: { path: string; why: string }[];
  errors: string[];
  freed_bytes: number;
}

export interface BulkDeleteResult {
  results: DeleteResult[];
  refused: { id: string; reason: string }[];
  freed_bytes: number;
  cleared?: number;
}

/** ``GET pool/cleanup``. */
export interface CleanupPartial {
  path: string;
  name: string;
  job: string | null;
  status: string | null;
  live: boolean;
  known: boolean;
  resumable: boolean;
  bytes: number;
  age_seconds: number;
  expires_in_seconds: number | null;
}

export interface CleanupJob {
  id: string;
  kind: string;
  status: string;
  bytes: number;
  age_seconds: number;
  expires_in_seconds: number;
}

export interface CleanupInventory {
  partials: CleanupPartial[];
  jobs: CleanupJob[];
  temp: { path: string; bytes: number; age_seconds: number }[];
  reclaimable_bytes: number;
  removable_bytes: number;
  disk: PoolDisk[];
  ttl: { partial_seconds: number; job_seconds: number };
}
