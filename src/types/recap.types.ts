/**
 * RECAP value-model advantage labels (annotation backend `recap/*` routes).
 *
 * A value model V(o_t) in [-1, 0] scores every frame of an episode; the
 * advantage A_t (value `lookahead` frames later minus value now, plus reward)
 * is compared with a threshold to give each frame a positive/negative label.
 * These shapes mirror the backend contract one to one.
 */

export interface RecapCheckpoint {
  name: string;
  provider: "rlinf" | "fake" | string;
  ready: boolean;
  reason: string | null;
  created_at: number | null;
  step: number | null;
  notes: string | null;
}

export type RecapThresholdSource = "checkpoint" | "dataset_quantile" | "manual";

/** How advantage labels are made: "rollout" reads each episode's outcome,
 * "sft" treats every episode as a success (demonstrations), "value_only"
 * computes V(o_t) without outcomes, advantages or labels. */
export type RecapDatasetType = "rollout" | "sft" | "value_only";

/** What a run with `dataset_type: "auto"` resolves to now, and why. */
export interface RecapDatasetTypeChoice {
  /** The person's dataset-level setting, or "auto" when none is stored. */
  setting: RecapDatasetType | "auto" | string;
  dataset_type: RecapDatasetType | string;
  /** user | manifest | outcomes | fallback | request */
  source: string;
  reason: string | null;
}

/** The dataset's current (latest successful) advantage-label revision. */
export interface RecapCurrent {
  /** The result's reference: the value model's name ("models" layout) or an
   * original per-run revision id ("revisions" layout). */
  revision_id: string;
  /** The value model (checkpoint) name. */
  model?: string | null;
  /** Changes on every recomputation; a read pinned to an older version is
   * answered 409 ("recomputed"). Equal to the revision id for a per-run
   * revision. Absent from older backends. */
  version?: string | null;
  /** "models": one result per value model; "revisions": one per run. */
  layout?: "models" | "revisions" | string | null;
  checkpoint: string;
  provider: string;
  created_at: number;
  episodes: number;
  frames: number;
  threshold: number;
  threshold_source: RecapThresholdSource | string;
  positive_quantile: number | null;
  lookahead: number;
  positive_fraction: number | null;
  stale: boolean;
  dataset_type?: RecapDatasetType | string | null;
  /** False for a values-only result: no advantages, thresholds or labels. */
  labels?: boolean;
  /** Set when the training data's static-pose filter was applied: only the
   * kept frames are labelled. */
  static_filter?: {
    rule: string | null;
    kept_frames: number | null;
    frames: number | null;
  } | null;
  /** Base-model folders not verified as official files (development only). */
  dev_only_base_models?: string[];
}

/** One saved run, independent of which result the dataset currently opens. */
export interface RecapRevision extends RecapCurrent {
  step: number | null;
  return_min: number | null;
  return_max: number | null;
  current: boolean;
  gamma?: number | null;
  failure_reward?: number | null;
  precision?: string | null;
  value_support?: {
    num_bins?: number | null;
    v_min?: number | null;
    v_max?: number | null;
  } | null;
}

export interface RecapRevisions {
  current: string | null;
  /** Newest first. Reading or selecting a row never changes current. */
  revisions: RecapRevision[];
  /** The same list under its newer name (`/api/recap/results`). */
  results?: RecapRevision[];
}

export interface RecapComparisonMetric {
  mean_a: number;
  mean_b: number;
  mean_abs_diff: number;
  corr: number | null;
}

/** Metrics use matching (episode, frame_index) pairs only. */
export interface RecapComparison {
  dataset: string;
  a: RecapRevision;
  b: RecapRevision;
  episodes: { shared: number; only_a: number; only_b: number };
  frames: { shared: number; only_a: number; only_b: number };
  labels: {
    agreement: number;
    positive_a_only: number;
    positive_b_only: number;
    both_positive: number;
    both_negative: number;
    positive_fraction_a: number;
    positive_fraction_b: number;
  } | null;
  value: RecapComparisonMetric | null;
  advantage: RecapComparisonMetric | null;
  /** Optional scale-aligned values using each run's saved return range. */
  value_return_units?: RecapComparisonMetric | null;
  per_episode: {
    episode: number;
    outcome: string | null;
    frames: number;
    /** Null when either side is a values-only result. */
    label_agreement: number | null;
    positive_fraction_a: number | null;
    positive_fraction_b: number | null;
    mean_value_a: number;
    mean_value_b: number;
    value_mean_abs_diff: number;
    value_corr: number | null;
  }[];
  notes: string[];
  outcome_separation: {
    auc_a: number;
    auc_b: number;
    success_episodes: number;
    failure_episodes: number;
  } | null;
  /** Shared episodes grouped by saved outcome ("unknown" when the runs saved
   * none or different ones); absent from older backends. */
  by_outcome?: RecapOutcomeGroup[];
  /** Binned counts of the shared frames; absent from older backends. */
  distribution?: RecapDistribution | null;
}

export interface RecapOutcomeGroup {
  outcome: "success" | "failure" | "unknown" | string;
  episodes: number;
  frames: number;
  /** Mean of the episodes' mean V. */
  mean_value_a: number;
  mean_value_b: number;
  /** Frame-weighted; null when either side has no labels. */
  label_agreement: number | null;
  positive_fraction_a: number | null;
  positive_fraction_b: number | null;
}

export interface RecapDistribution {
  bins: number;
  /** V of A and of B on common bin edges (edges has bins + 1 entries). */
  value: { edges: number[]; a: number[]; b: number[] };
  /** |B − A| per shared frame. */
  abs_diff: { edges: number[]; counts: number[] };
}

export type RecapJobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface RecapJob {
  id: string;
  repo_id: string;
  checkpoint: string;
  status: RecapJobState;
  progress: {
    stage: "loading" | "values" | "advantages" | "saving" | string;
    done: number;
    total: number;
  };
  error: string | null;
  revision_id: string | null;
  created_at: number;
}

export interface RecapStatus {
  checkpoints: RecapCheckpoint[];
  worker: { ready: boolean; reason: string | null };
  current: RecapCurrent | null;
  job: RecapJob | null;
  /** What a run resolves its label rule to; absent from older backends. */
  dataset_type?: RecapDatasetTypeChoice | null;
}

export interface RecapRunRequest {
  checkpoint: string;
  /** Omitted by the viewer: the backend decides ("auto"). */
  dataset_type?: "auto" | RecapDatasetType;
  episodes?: number[] | null;
  lookahead?: number;
  positive_quantile?: number;
  threshold?: number | null;
}

export interface RecapEpisodeSummary {
  positive_fraction: number;
  mean_advantage: number;
  mean_value: number;
  frames: number;
}

export interface RecapSummary {
  revision_id: string;
  version?: string | null;
  threshold: number | null;
  episodes: Record<string, RecapEpisodeSummary>;
}

/** Per-frame labels for one episode; all arrays share one length. */
export interface RecapEpisode {
  episode_index: number;
  revision_id: string;
  version?: string | null;
  /** False for a values-only result: `advantage` and `positive` are empty. */
  labels?: boolean;
  fps: number;
  threshold: number | null;
  frame_index: number[];
  timestamp: number[];
  value: number[];
  advantage: number[];
  positive: boolean[];
  /** True when only the frames kept by the training static filter are
   * listed; the frame indices then skip the unlabelled (dropped) frames. */
  static_filter?: boolean;
  /** Frames in the episode (labelled or not). */
  episode_frames?: number | null;
}

/** Dispatched on `window` when a run finishes, so other views (the episode
 * list's positive-fraction badges) can refresh. */
export const RECAP_UPDATED_EVENT = "levi-recap-updated";

/** Dispatched on `window` after a person's outcome label is saved: results
 * computed before it are stale, so the VALUE MODEL header re-reads status. */
export const OUTCOME_LABELS_CHANGED_EVENT = "levi-outcome-labels-changed";

export function isRecapJobActive(job: RecapJob | null | undefined): boolean {
  return !!job && (job.status === "queued" || job.status === "running");
}
