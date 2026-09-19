// Mirrors levi/describe.py: what a registered dataset is and what LEVI can
// do with it.

export type DatasetOrigin =
  | "raw_capture"
  | "levi_conversion"
  | "levi_recap"
  | "levi_export"
  | "external";

export interface DatasetCapabilities {
  browse: boolean;
  annotate: boolean;
  outcome_labels: boolean;
  sam3: boolean;
  doctor: boolean | "view";
  export_annotated: boolean;
  push_to_hub: boolean;
  convert: boolean;
}

export interface DatasetFormat {
  kind: "raw" | "lerobot";
  origin: DatasetOrigin;
  version: string | null;
  fps: number | null;
  episodes: number | null;
  frames: number | null;
  robot_type?: string | null;
  input_format?: string | null;
  variant?: string | null;
  view_status?: "building" | "ready" | "failed" | null;
  view_fps?: number | null;
  excluded?: number;
  timing?: "resample" | "retime" | null;
  filter_static?: boolean | null;
  source?: string | null;
  recap?: {
    dataset_type?: string;
    failure_reward?: number;
    gamma?: number;
    outcomes?: Record<string, number>;
  };
  capabilities: DatasetCapabilities;
}

export interface CatalogEntry {
  id: string;
  name: string;
  path: string;
  kind?: "lerobot" | "raw";
  view?: string;
  view_status?: "building" | "ready" | "failed";
  view_error?: string;
  format?: DatasetFormat;
  /** Metadata signature; changes when the dataset changes on disk. */
  revision?: string | null;
  registered_by?: "sync" | "user";
  info?: {
    total_episodes: number;
    total_frames: number;
    codebase_version: string;
  };
}
