/** Model-neutral SAM3 sidecar records returned by the LEVI API. */

export type ReviewStatus =
  | "suggested"
  | "accepted"
  | "rejected"
  | "needs_review";

export interface ObjectAnnotation {
  episode_index: number;
  frame_index: number;
  timestamp: number;
  camera_key: string;
  object_id: string;
  track_id: number;
  concept: string;
  category: string | null;
  bbox_xyxy: [number, number, number, number];
  image_size: [number, number];
  mask_rle: { size: [number, number]; counts: number[] };
  score: number;
  visible: boolean;
  occluded: boolean;
  status: ReviewStatus;
  source: "sam3" | "human" | "fake" | "import";
  prompt: string | null;
}

export interface ObjectTrack {
  episode_index: number;
  camera_key: string;
  track_id: number;
  object_id: string;
  concept: string;
  category: string | null;
  start_frame: number;
  end_frame: number;
  mean_score: number;
  min_score: number;
  status: ReviewStatus;
  lineage: string[];
}

export interface Sam3Plan {
  /** Set only on a run request to reuse the plan returned by preflight. */
  plan_id?: string | null;
  repo_id?: string | null;
  local_path?: string | null;
  revision?: string | null;
  episode_indices: number[];
  camera_keys: string[];
  prompts: string[];
  start_frame?: number;
  max_frames?: number | null;
  review_threshold?: number;
  accept_threshold?: number;
  provider?: "sam3" | "fake";
}

export type Sam3DownloadPhase = "idle" | "downloading" | "ready" | "error";

export interface Sam3DownloadStatus {
  phase: Sam3DownloadPhase | string;
  repo_id?: string;
  filename?: string;
  revision?: string;
  bytes?: number;
  total_bytes?: number | null;
  percent?: number | null;
  path?: string;
  message?: string;
  updated_at?: number;
}

export interface Sam3AuthStatus {
  authenticated: boolean;
  username: string | null;
  source: "browser" | "environment/cache" | "none" | string;
}

export interface Sam3Capabilities {
  provider: "sam3";
  enabled: boolean;
  worker_project_present: boolean;
  worker_python_present: boolean;
  requires_user_checkpoint_access: boolean;
  gpu_probe_performed: false;
  manual_annotation_available: boolean;
  model_repo?: string;
  model_filename?: string;
  model_revision?: string;
  checkpoint_dir?: string;
  checkpoint_path?: string;
  checkpoint_cached?: boolean;
  checkpoint_size_bytes?: number;
  download?: Sam3DownloadStatus;
  hf_auth?: Sam3AuthStatus;
  message: string;
}

/** Episode/camera-pair progress through a running batch annotation job. */
export interface Sam3JobProgress {
  done: number;
  total: number;
  current_episode?: number | null;
  current_camera?: string | null;
  updated_at?: number;
}

/** One (episode, camera) pair that failed inside an otherwise-successful batch. */
export interface Sam3ItemError {
  episode_index: number;
  camera_key: string;
  error: string;
}

export interface Sam3JobStatus {
  job_id?: string;
  status?: string;
  provider?: "sam3" | "fake" | string;
  plan_id?: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  revision_id?: string;
  annotation_count?: number;
  error?: string;
  // Tail of the worker's stdout/stderr log when it exited without writing a
  // result (e.g. a Python traceback for an unhandled crash such as CUDA OOM).
  error_detail?: string;
  progress?: Sam3JobProgress;
  item_errors?: Sam3ItemError[];
}

/** A named, reusable set of SAM3 text prompts, shared across datasets. */
export interface Sam3PromptPreset {
  name: string;
  prompts: string[];
}

export interface Sam3Revision {
  schema_version: string;
  revision_id: string;
  parent_revision: string | null;
  created_at: string;
  annotation_count: number;
  track_count: number;
  model: Record<string, unknown>;
}

export interface Sam3Edit {
  episode_index: number;
  camera_key: string;
  operation:
    | "accept"
    | "reject"
    | "relabel"
    | "occlusion"
    | "delete"
    | "split"
    | "merge"
    | "refine";
  object_id?: string;
  track_id?: number;
  frame_index?: number;
  concept?: string;
  visible?: boolean;
  occluded?: boolean;
  payload?: Record<string, unknown>;
  base_revision?: string | null;
}

export type ObjectEdit = Sam3Edit;
