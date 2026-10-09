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

/** The dataset's current (latest successful) advantage-label revision. */
export interface RecapCurrent {
  revision_id: string;
  checkpoint: string;
  provider: string;
  created_at: number;
  episodes: number;
  frames: number;
  threshold: number;
  threshold_source: RecapThresholdSource | string;
  positive_quantile: number | null;
  lookahead: number;
  positive_fraction: number;
  stale: boolean;
  dataset_type?: string | null;
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
    label_agreement: number;
    positive_fraction_a: number;
    positive_fraction_b: number;
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
}

export interface RecapRunRequest {
  checkpoint: string;
  dataset_type?: "sft" | "rollout";
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
  threshold: number;
  episodes: Record<string, RecapEpisodeSummary>;
}

/** Per-frame labels for one episode; all arrays share one length. */
export interface RecapEpisode {
  episode_index: number;
  revision_id: string;
  fps: number;
  threshold: number;
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
