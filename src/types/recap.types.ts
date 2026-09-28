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
