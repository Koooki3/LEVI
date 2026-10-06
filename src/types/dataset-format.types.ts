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
  source_time_error_max_seconds?: number | null;
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

/** A dataset of a live evaluation workspace shown here read-only
 * (levi/links.py): never copied, never written from this LEVI. */
export interface LinkedDataset {
  kind: "live";
  /** The dataset's name in the live workspace's own catalog. */
  source: string;
  /** The live workspace's folder name (never its path). */
  workspace: string;
  readonly: true;
}

/** What the catalog says is linked: one row per live workspace. */
export interface LinkedWorkspace {
  kind: "live";
  workspace: string;
  datasets: number;
}

export interface CatalogEntry {
  id: string;
  name: string;
  path: string;
  /** Set when this entry is a live evaluation workspace's dataset. */
  linked?: LinkedDataset;
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
