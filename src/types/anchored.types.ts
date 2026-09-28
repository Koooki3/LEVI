/**
 * Anchored review results (annotation backend `anchored/*` routes; see
 * levi/agent/anchored.py). One record per episode of the newest anchored
 * review run: every recorded robot event (a gripper opening, say), the
 * answers the model gave about it, how each condition of the spec's rule read
 * and whether the event counts. These shapes mirror the backend contract.
 */

export type AnchoredVerdict = "supported" | "contradicted" | "unknown";

export interface AnchoredCheck {
  field: string;
  value: string;
  result: AnchoredVerdict;
}

export interface AnchoredFrame {
  role: string;
  camera: string;
  offset: number;
  frame_index: number;
  evidence_id: string;
}

export interface AnchoredEvent {
  frame_index: number;
  timestamp: number;
  answer: Record<string, string>;
  checks: AnchoredCheck[];
  verdict: AnchoredVerdict;
  valid: boolean;
  frames: AnchoredFrame[];
}

export interface AnchoredEpisode {
  schema: string;
  run_id: string;
  episode_index: number;
  status: string;
  spec: { id: string; version: number | null };
  channel: string;
  event: "open" | "close" | string;
  outcome: "success" | "failure";
  basis: {
    valid_labels?: string[];
    missing_labels?: string[];
    undecided_labels?: string[];
    valid_events?: number;
  };
  events: AnchoredEvent[];
}
