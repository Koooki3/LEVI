// Read-only contract of levi/live/result_index.py; evaluation and annotation runs differ.
import type { SessionIdentity } from "./session-route";
export type ResultStatus =
  | "active"
  | "waiting_annotation"
  | "partial"
  | "reported"
  | "stale"
  | "missing_source"
  | "archived"
  | "rebound"
  | "unknown"
  | "blocked";
export interface AnalysisShare {
  n: number;
  of: number;
  rate: number | null;
  wilson95?: [number, number] | null;
  small_sample?: boolean;
}
export interface AnalysisCell {
  group?: string | null;
  task?: string | null;
  episodes: number;
  coverage: {
    operator: AnalysisShare;
    background: AnalysisShare;
    segments: AnalysisShare;
  };
  success: AnalysisShare;
  background_success: AnalysisShare;
  funnel: {
    of: number;
    stages: { id: string; reached: AnalysisShare; present: number }[];
  };
  warnings: { code: string; n?: number; min?: number; rate?: number }[];
}
export interface AnalysisRecord {
  dataset: string;
  demo: string;
  episode_index: number | null;
  session: string | null;
  operator: { outcome?: string | null } | null;
  online: { outcome?: string | null; status?: string | null } | null;
  agent: {
    outcome?: string | null;
    review?: string | null;
    rule?: string | null;
    source?: string | null;
  } | null;
  segments: {
    count: number;
    items: {
      id: string;
      start: number | null;
      end: number | null;
      outcome?: string | null;
    }[];
  } | null;
  spec: {
    guideline?: string | null;
    release_review?: string | null;
    release_review_version?: string | null;
  } | null;
}
export interface ResultViewer {
  dataset: string;
  repo_id: string | null;
  native_repo_id: string | null;
  viewer_href: string | null;
  annotation_base: string | null;
  status: "ready" | "unknown";
}
export interface ResultRun {
  dataset: string;
  kind: "temporal" | "verdict";
  run_id: string;
  changeset: string | null;
  episodes: number[];
  repo_id: string | null;
  viewer_href: string | null;
  annotation_href: string | null;
}
export interface ComparisonConditions {
  reset_mode: string | null;
  label_basis: string;
  stop_rules: {
    consecutive_faults: number;
    unplanned_interventions_per_arm: number;
  } | null;
  coverage: Record<
    string,
    {
      valid: number;
      labelled: number;
      unlabelled: number;
      coverage: number | null;
    }
  > | null;
  early_stop: {
    truth_basis?: string;
    arms?: Record<
      string,
      {
        episodes: number;
        controls: number;
        false_early_stop_rate: {
          rate: number | null;
          k: number | null;
          n: number;
          status: string;
        };
      }
    >;
  } | null;
  runs: {
    run_id: string | null;
    reset_mode: string | null;
    scene_check: string | null;
    levi_commit: string | null;
    plan_sha256: string | null;
  }[];
  arms: {
    id: string | null;
    config: string | null;
    checkpoint: string | null;
    versions: string | null;
    not_verified: string[] | null;
  }[];
}
export interface ResultItem {
  identity: {
    root: string;
    workspace: string;
    group: string;
    task: string;
    session_id: string | null;
    run_id: string | null;
    datasets: string[];
  };
  status: ResultStatus;
  reason: string | null;
  result_visibility: "visible" | "withheld" | "unknown";
  statistics_visibility: "visible" | "withheld" | "unknown";
  source: {
    kind: "current_session" | "catalog_reference";
    catalog_versions?: {
      basis: string;
      version: string | null;
      status: "unverified";
    }[];
    snapshot: string | null;
    basis: "operator_and_automatic_separate";
    missing: string[];
  };
  policy: { config: string | null; checkpoint: string | null };
  label_mode: string | null;
  analysis: { schema: string; all: AnalysisCell; cells: AnalysisCell[] } | null;
  records: AnalysisRecord[];
  viewers: ResultViewer[];
  runs: ResultRun[];
  reports: {
    basis: string;
    version: string | null;
    files: string[];
    current?: boolean;
    status?: "current" | "stale" | "missing" | "unknown";
    download_url?: string | null;
  }[];
  comparison: {
    status: "unknown" | "incompatible" | "comparable";
    campaign_id: string | null;
    missing: string[];
    differences?: string[];
    conditions?: ComparisonConditions | null;
    cohort?: string | null;
    settings_sha256?: string | null;
  };
}
export interface ResultIndex {
  schema: "levi.live.result-index.v1";
  available: boolean;
  items: ResultItem[];
  complete?: boolean;
  truncated?: string[];
  limits?: Record<string, number>;
}
export function resultIdentity(item: ResultItem): SessionIdentity {
  return {
    root: item.identity.root,
    group: item.identity.group,
    task: item.identity.task,
    id: item.identity.session_id ?? "",
    run: item.identity.run_id ?? "",
    dataset: item.identity.datasets[0] ?? "",
  };
}
