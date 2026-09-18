// Mirrors levi/conversion/report.py and the job records in levi/jobs.py.

export type RequirementStatus = "pass" | "warn" | "fail" | "info";

export interface Requirement {
  id: string;
  label: string;
  status: RequirementStatus;
  verified: "now" | "during_scan";
  detail: string;
  fix: string;
}

export interface EpisodeFinding {
  source_id: string;
  frames: number | null;
  outcome: string | null;
  errors: string[];
  warnings: string[];
}

export interface Solution {
  id: string;
  label: string;
  options: Record<string, unknown>;
  link: string | null;
}

export interface TargetCompatibility {
  target: string;
  label: string;
  status: "supported" | "warnings" | "unsupported";
  reasons: string[];
  solutions: Solution[];
  defaults: Record<string, unknown>;
}

export interface InputReport {
  source: string;
  format: string | null;
  label: string;
  variant: string | null;
  summary: {
    demos?: number;
    frames?: number;
    tasks?: Record<string, number>;
    outcomes?: Record<string, number>;
    fps_min?: number | null;
    fps_max?: number | null;
    output_fps?: number | null;
    fps_note?: string | null;
    discarded?: number;
    hint?: string;
  };
  requirements: Requirement[];
  episodes: EpisodeFinding[];
  targets: TargetCompatibility[];
}

export interface Progress {
  stages: string[];
  stage: string;
  stage_index: number;
  done: number;
  total: number;
  current: string;
  warnings: string[];
  started_at: number;
  updated_at: number;
  elapsed_seconds: number;
  eta_seconds: number | null;
}

export interface Job {
  id: string;
  stage: string;
  source: string;
  output: string;
  argv: string[];
  status: string;
  options?: Record<string, unknown>;
  log?: string;
  error?: string;
  dataset?: string;
  exit_code?: number;
  output_exists?: boolean;
  result?: Record<string, unknown>;
  progress?: Progress | null;
}

export interface FormatDescription {
  id: string;
  label: string;
  description: string;
  evidence: string;
  convertible?: boolean;
  defaults?: Record<string, unknown>;
  inputs?: string[];
}

export interface Formats {
  inputs: FormatDescription[];
  outputs: FormatDescription[];
  unsupported: {
    id: string;
    label: string;
    reason: string;
    workaround: string;
  }[];
  matrix: Record<string, string[]>;
}

/** The subset of levi/conversion/options.py the wizard edits directly. */
export interface ConversionOptions {
  fps: number;
  source_fps: number;
  timing: "resample" | "retime";
  filter_static: boolean;
  workers: number | null;
  keep_intermediates: boolean;
  exclude_demos: string[];
  target: string;
  target_options: Record<string, unknown>;
  [key: string]: unknown;
}
