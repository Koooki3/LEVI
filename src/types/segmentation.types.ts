/** Fast instance segmentation (`/api/segmentation/*`): a distilled student
 * model that labels datasets offline and follows the player live. */

export interface SegReadiness {
  ready: boolean;
  reason: string | null;
}

export interface SegModelMetrics {
  ap50: number | null;
  ap: number | null;
  recall_class_agnostic: number | null;
  concepts: Record<string, number | null>;
  images: number | null;
}

export interface SegModel {
  name: string;
  provider?: string;
  architecture: string | null;
  concepts: string[];
  datasets: string[];
  created_at: string | null;
  teacher: string | null;
  confidence: number | null;
  metrics: SegModelMetrics;
  training_frames?: Record<string, number> | null;
  licence?: Record<string, string> | null;
  ready: boolean;
  path: string;
  for_this_dataset?: boolean;
}

export type SegJobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface SegProgress {
  stage?: string;
  done?: number;
  total?: number;
  [key: string]: unknown;
}

export interface SegItemError {
  episode_index?: number;
  camera_key?: string;
  error?: string;
}

export interface SegJob {
  id: string;
  kind: "label" | "distil";
  provider: string;
  model: string | null;
  status: SegJobStatus;
  progress: SegProgress | null;
  error: string | null;
  error_detail?: string | null;
  revision_id: string | null;
  annotation_count: number | null;
  item_errors: SegItemError[] | null;
  created_at: number | string | null;
  finished_at: number | string | null;
  warnings: string[] | null;
  timing: {
    frames?: number;
    seconds?: number;
    load_seconds?: number;
    fps?: number | null;
    [key: string]: unknown;
  } | null;
  request: Record<string, unknown> | null;
}

export type LiveState =
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "failed";

export interface LiveCameraStats {
  fps?: number;
  decoded?: number;
  processed?: number;
  skipped?: number;
  dropped?: number;
  latency_ms_p50?: number | null;
  latency_ms_p95?: number | null;
  last_frame?: number | null;
}

export interface LiveStats {
  cameras?: Record<string, LiveCameraStats>;
  infer_ms_p50?: number | null;
  vram_process_mib?: number | null;
  vram_peak_mib?: number | null;
}

export interface LiveSessionInfo {
  id: string;
  episode_index: number;
  cameras: string[];
  model: string | null;
  provider?: string;
  save?: boolean;
  state: LiveState;
  error: string | null;
  ready: {
    engine?: unknown;
    fps?: number;
    frames?: number;
    load_s?: number;
  } | null;
  stats: LiveStats | null;
  summary: unknown;
  revision_id: string | null;
  annotation_count: number | null;
}

export interface SegStatus {
  worker: SegReadiness;
  teacher: SegReadiness & { enabled: boolean };
  gpu_lock: boolean;
  models: SegModel[];
  cameras: string[];
  fps: number | null;
  episodes: number;
  jobs: SegJob[];
  live: LiveSessionInfo[];
}

export interface LiveObject {
  track_id: number;
  concept: string;
  score: number;
  bbox_xyxy: [number, number, number, number];
  mask_rle: { size: [number, number]; counts: number[] } | null;
}

/** One camera frame from the live worker (`result` event). */
export interface LiveResult {
  camera_key: string;
  frame_index: number;
  timestamp: number;
  latency_ms: number;
  image_size: [number, number] | null;
  objects: LiveObject[];
}

export interface DistilSourceRequest {
  repo_id?: string | null;
  train: number[];
  valid: number[];
  test: number[];
  stride?: number;
}

export interface DistilRequest {
  name: string;
  concepts: string[];
  sources: DistilSourceRequest[];
  architecture?: string;
  epochs?: number;
  stride?: number;
  confidence?: number;
  note?: string;
}
