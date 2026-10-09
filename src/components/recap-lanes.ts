/**
 * Pure geometry for the timeline's VALUE MODEL section (RECAP advantage
 * labels). No React, no DOM — everything here is unit-tested.
 *
 * - `advantageRuns` merges consecutive frames with the same positive/negative
 *   label into segments the ADVANTAGE row draws.
 * - `valuePath` maps V(o_t) in [-1, 0] onto an SVG polyline for the VALUE row.
 * - `nearestFrame` finds the frame under the playhead for the value readout.
 *
 * The backend returns frames sorted by timestamp; the helpers still tolerate
 * unsorted input, mismatched array lengths and non-finite numbers so a bad
 * payload degrades to fewer marks rather than a broken timeline.
 */

import type { RecapRevision, RecapRevisions } from "@/types/recap.types";

export interface AdvantageRun {
  /** Seconds: timestamp of the run's first frame. */
  start: number;
  /** Seconds: last frame's timestamp + one frame period. */
  end: number;
  positive: boolean;
  /** Mean continuous advantage over the run's finite values; null if none. */
  meanAdv: number | null;
  /** 0.45..1 — mean |A − threshold| of the run over the episode's scale. */
  strength: number;
  firstFrame: number;
  lastFrame: number;
  /** Number of frames merged into this run. */
  count: number;
}

export const MIN_STRENGTH = 0.45;

const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

/** Indices of frames with a finite timestamp, ordered by timestamp (stable). */
export function frameOrder(timestamps: readonly number[], length?: number) {
  const n = Math.min(length ?? timestamps.length, timestamps.length);
  const order: number[] = [];
  for (let i = 0; i < n; i++) if (finite(timestamps[i])) order.push(i);
  let sorted = true;
  for (let k = 1; k < order.length; k++)
    if (timestamps[order[k]] < timestamps[order[k - 1]]) {
      sorted = false;
      break;
    }
  if (!sorted) order.sort((a, b) => timestamps[a] - timestamps[b] || a - b);
  return order;
}

/** One frame period in seconds: 1/fps, or the median timestamp step when
 * fps is missing or invalid (0 when neither is known). */
export function framePeriod(
  fps: number,
  timestamps: readonly number[] = [],
): number {
  if (finite(fps) && fps > 0) return 1 / fps;
  const order = frameOrder(timestamps);
  const steps: number[] = [];
  for (let k = 1; k < order.length; k++) {
    const step = timestamps[order[k]] - timestamps[order[k - 1]];
    if (step > 0) steps.push(step);
  }
  if (steps.length === 0) return 0;
  steps.sort((a, b) => a - b);
  return steps[Math.floor(steps.length / 2)];
}

/** Scale that maps |A − threshold| to strength 1: the 95th percentile over the
 * episode (so one outlier frame does not wash every other run out), falling
 * back to the maximum. */
function advantageScale(
  advantage: readonly number[],
  threshold: number,
  order: readonly number[],
): number {
  const gaps: number[] = [];
  for (const i of order) {
    const a = advantage[i];
    if (finite(a)) gaps.push(Math.abs(a - threshold));
  }
  if (gaps.length === 0) return 0;
  gaps.sort((a, b) => a - b);
  const p95 = gaps[Math.min(gaps.length - 1, Math.floor(gaps.length * 0.95))];
  return p95 > 0 ? p95 : gaps[gaps.length - 1];
}

/**
 * Merge consecutive frames with the same label into runs. A run also breaks
 * where the frame indices skip (a missing frame is not silently bridged).
 */
export function advantageRuns(
  frames: readonly number[],
  positive: readonly boolean[],
  advantage: readonly number[],
  timestamps: readonly number[],
  fps: number,
  threshold = 0,
): AdvantageRun[] {
  const n = Math.min(timestamps.length, positive.length);
  const order = frameOrder(timestamps, n);
  if (order.length === 0) return [];
  const thr = finite(threshold) ? threshold : 0;
  const period = framePeriod(fps, timestamps);
  const scale = advantageScale(advantage, thr, order);

  const runs: AdvantageRun[] = [];
  let first = 0; // position in `order` where the current run starts
  const frameOf = (i: number) => (finite(frames[i]) ? frames[i] : i);

  const close = (from: number, to: number) => {
    let sum = 0;
    let gap = 0;
    let finiteCount = 0;
    for (let k = from; k <= to; k++) {
      const a = advantage[order[k]];
      if (!finite(a)) continue;
      sum += a;
      gap += Math.abs(a - thr);
      finiteCount++;
    }
    const i0 = order[from];
    const i1 = order[to];
    const meanGap = finiteCount ? gap / finiteCount : 0;
    const strength =
      finiteCount === 0
        ? MIN_STRENGTH
        : scale > 0
          ? Math.max(MIN_STRENGTH, Math.min(1, meanGap / scale))
          : 1;
    runs.push({
      start: timestamps[i0],
      end: timestamps[i1] + period,
      positive: positive[i0] === true,
      meanAdv: finiteCount ? sum / finiteCount : null,
      strength,
      firstFrame: frameOf(i0),
      lastFrame: frameOf(i1),
      count: to - from + 1,
    });
  };

  for (let k = 1; k < order.length; k++) {
    const prev = order[k - 1];
    const cur = order[k];
    const sameLabel = (positive[cur] === true) === (positive[prev] === true);
    const contiguous =
      !finite(frames[cur]) ||
      !finite(frames[prev]) ||
      frames[cur] - frames[prev] <= 1;
    if (!sameLabel || !contiguous) {
      close(first, k - 1);
      first = k;
    }
  }
  close(first, order.length - 1);
  return runs;
}

/** Share of frames labelled positive (0..1); null for an empty episode. */
export function positiveFraction(positive: readonly boolean[]): number | null {
  if (positive.length === 0) return null;
  let count = 0;
  for (const p of positive) if (p === true) count++;
  return count / positive.length;
}

/** Map a value in [-1, 0] to a y coordinate: 0 at the top, -1 at the bottom.
 * Out-of-range values are clamped onto the plot. */
export function valueToY(value: number, height: number): number {
  const v = Math.max(-1, Math.min(0, value));
  return -v * height;
}

const round = (x: number) => Math.round(x * 100) / 100;

/**
 * SVG `points` for a polyline of V(o_t) over [0, duration] mapped onto a
 * `width` × `height` box. Frames with a non-finite value or timestamp are
 * skipped; returns "" when nothing can be drawn.
 */
export function valuePath(
  values: readonly number[],
  timestamps: readonly number[],
  duration: number,
  width: number,
  height: number,
): string {
  if (!(duration > 0) || !(width > 0) || !(height > 0)) return "";
  const order = frameOrder(timestamps, values.length);
  const points: string[] = [];
  for (const i of order) {
    const v = values[i];
    if (!finite(v)) continue;
    const x = Math.max(0, Math.min(width, (timestamps[i] / duration) * width));
    points.push(`${round(x)},${round(valueToY(v, height))}`);
  }
  return points.join(" ");
}

/** Separate polylines at missing frames: a static-filter gap stays visible. */
export function valuePaths(
  values: readonly number[],
  timestamps: readonly number[],
  frames: readonly number[],
  duration: number,
  width: number,
  height: number,
): string[] {
  if (!(duration > 0) || !(width > 0) || !(height > 0)) return [];
  const paths: string[] = [];
  let points: string[] = [];
  let previous: number | null = null;
  const close = () => {
    if (points.length) paths.push(points.join(" "));
    points = [];
    previous = null;
  };
  for (const i of frameOrder(timestamps, values.length)) {
    const v = values[i];
    const frame = finite(frames[i]) ? frames[i] : i;
    if (!finite(v)) {
      close();
      continue;
    }
    if (previous != null && frame !== previous + 1) close();
    const x = Math.max(0, Math.min(width, (timestamps[i] / duration) * width));
    points.push(`${round(x)},${round(valueToY(v, height))}`);
    previous = frame;
  }
  close();
  return paths;
}

/**
 * Index of the frame whose timestamp is closest to `t` (ties go to the
 * earlier frame); -1 when there are no finite timestamps. Binary search on
 * sorted input, linear scan otherwise.
 */
export function nearestFrame(timestamps: readonly number[], t: number): number {
  if (!finite(t)) return -1;
  const n = timestamps.length;
  let sorted = true;
  for (let i = 1; i < n; i++)
    if (!(timestamps[i] >= timestamps[i - 1])) {
      sorted = false;
      break;
    }
  if (sorted && n > 0) {
    let lo = 0;
    let hi = n - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (timestamps[mid] < t) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0 && t - timestamps[lo - 1] <= timestamps[lo] - t) return lo - 1;
    return lo;
  }
  let best = -1;
  let bestGap = Infinity;
  for (let i = 0; i < n; i++) {
    const ts = timestamps[i];
    if (!finite(ts)) continue;
    const gap = Math.abs(ts - t);
    if (gap < bestGap) {
      best = i;
      bestGap = gap;
    }
  }
  return best;
}

/** Timestamp is a frame's start, so its value applies on [start, start +
 * period). Return the saved array index, never a future frame or a dropped
 * frame's nearest neighbour. */
export function labelledFrameAtTime(
  timestamps: readonly number[],
  fps: number,
  time: number,
): number {
  if (!finite(time)) return -1;
  const period = framePeriod(fps, timestamps);
  if (!(period > 0)) return -1;
  let sorted = true;
  for (let i = 0; i < timestamps.length; i++) {
    if (
      !finite(timestamps[i]) ||
      (i > 0 && timestamps[i] < timestamps[i - 1])
    ) {
      sorted = false;
      break;
    }
  }
  let index = -1;
  if (sorted) {
    // Upper bound: the last saved frame whose start is at or before time.
    let lo = 0;
    let hi = timestamps.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (timestamps[mid] <= time) lo = mid + 1;
      else hi = mid;
    }
    index = lo - 1;
  } else {
    for (let i = 0; i < timestamps.length; i++) {
      const start = timestamps[i];
      if (
        finite(start) &&
        start <= time &&
        (index < 0 || start > timestamps[index])
      )
        index = i;
    }
  }
  if (index < 0) return -1;
  const start = timestamps[index];
  const end = start + period;
  // Decimal sums such as 0.2 + 0.1 exceed the exact boundary by one ULP.
  const epsilon =
    Number.EPSILON * Math.max(1, Math.abs(start), Math.abs(end)) * 4;
  return time < end - epsilon ? index : -1;
}

/** Stable backend warning codes to bilingual catalog keys. Unknown codes
 * still produce a visible, generic boundary instead of raw English. */
export function recapComparisonNoteKey(note: string): string {
  const keys: Record<string, string> = {
    return_range_differs:
      "Return ranges differ: normalized Value and advantage are not on the same original-return scale.",
    threshold_differs:
      "Thresholds differ; label flips can reflect the threshold as well as the model.",
    lookahead_differs:
      "Lookahead differs; continuous advantages use different future horizons.",
    gamma_differs:
      "Discount factors differ; continuous advantages use different reward weighting.",
    failure_reward_differs:
      "Failure rewards differ; continuous advantages include different reward penalties.",
    dataset_type_differs:
      "Dataset label rules differ; SFT labels may be forced positive.",
    static_filter_differs:
      "Static-filter coverage differs; metrics use only the frames labelled by both versions.",
    value_support_differs:
      "Value supports differ; predictions were discretized differently.",
    precision_differs: "Inference precision differs between these results.",
    outcomes_differ:
      "Outcome labels changed between runs; advantage differences can reflect those labels.",
    stale_results:
      "A selected result is stale; its data or outcome labels have changed since computation.",
    fingerprint_unavailable:
      "A saved result has no source fingerprint; identical frame IDs cannot confirm identical source data.",
    coverage_differs:
      "Frame coverage differs; metrics exclude frames absent from either version.",
  };
  return (
    keys[note] ??
    "These results have additional computation differences; interpret comparisons with care."
  );
}

/** Fixed-precision signed number for readouts, "—" for null/non-finite. */
export function formatSigned(value: number | null | undefined, digits = 3) {
  if (!finite(value)) return "—";
  const text = value.toFixed(digits);
  return value > 0 ? `+${text}` : text;
}

// ---------------------------------------------------------------------------
// Section state: what the VALUE MODEL header and rows show.

export interface RecapViewInput {
  /** recap/status has answered (successfully). */
  statusLoaded: boolean;
  /** recap/status failed (route missing, backend down, …). */
  statusError: boolean;
  /** status.checkpoints is non-empty (ready or not). */
  hasCheckpoints: boolean;
  /** status.current is set: the dataset has a result. */
  hasCurrent: boolean;
  jobActive: boolean;
  /** This episode's labels: still loading, absent (404), or present. */
  episode: "loading" | "none" | "labels";
  /** The user opened the compute controls under an existing result. */
  controlsOpen: boolean;
}

export interface RecapView {
  /** Muted one-line note in place of the header subtitle. */
  note:
    | "unavailable"
    | "loading"
    | "no-checkpoint"
    | "no-episode-labels"
    | null;
  /** What the right side of the header offers. */
  control: "progress" | "compute" | "recompute" | null;
  /** Checkpoint · threshold · positive share + legend in the header. */
  showMeta: boolean;
  /** ADVANTAGE + VALUE rows (empty while an episode loads under a result,
   * so the timeline keeps its height). */
  showRows: boolean;
}

export function recapView(input: RecapViewInput): RecapView {
  const hasLabels = input.episode === "labels";
  const showRows =
    hasLabels || (input.episode === "loading" && input.hasCurrent);
  let note: RecapView["note"] = null;
  if (!hasLabels) {
    if (input.statusError) note = "unavailable";
    else if (!input.statusLoaded) note = "loading";
    else if (!input.hasCheckpoints && !input.hasCurrent && !input.jobActive)
      note = "no-checkpoint";
    else if (input.hasCurrent && input.episode === "none")
      note = "no-episode-labels";
  }
  let control: RecapView["control"] = null;
  if (input.jobActive) control = "progress";
  else if (input.statusLoaded && input.hasCheckpoints)
    control = input.hasCurrent && !input.controlsOpen ? "recompute" : "compute";
  return { note, control, showMeta: hasLabels, showRows };
}

/** English catalog key for where a result's threshold came from (the
 * header's tooltip); unknown sources fall back to the raw value. */
export function thresholdSourceKey(source: string | null | undefined): string {
  switch (source) {
    case "manual":
      return "threshold set by hand for this run";
    case "checkpoint":
      return "the checkpoint's unified threshold";
    case "dataset_quantile":
      return "quantile of this dataset's advantages";
    default:
      return source ?? "";
  }
}

/** Only registered local datasets (`local/<name>`) can have advantage
 * labels; the section stays hidden for Hub datasets. */
export function recapApplies(repoId: string | null | undefined): boolean {
  return !!repoId && repoId.startsWith("local/");
}

export interface RecapSelection {
  primary: string;
  comparison: string;
}

/** Preserve a person's historical selection when a new run is published;
 * default only when that selection is absent from this dataset. */
export function recapSelection(
  results: RecapRevisions,
  previous: RecapSelection,
): RecapSelection {
  const ids = new Set(results.revisions.map((row) => row.revision_id));
  const primary = ids.has(previous.primary)
    ? previous.primary
    : results.current && ids.has(results.current)
      ? results.current
      : (results.revisions[0]?.revision_id ?? "");
  return {
    primary,
    comparison:
      ids.has(previous.comparison) && previous.comparison !== primary
        ? previous.comparison
        : "",
  };
}

/** Catalog keys explain boundaries on numerical and label comparisons. */
export function recapComparisonWarnings(
  a: RecapRevision,
  b: RecapRevision,
): string[] {
  const warnings: string[] = [];
  if (
    !finite(a.return_min) ||
    !finite(a.return_max) ||
    !finite(b.return_min) ||
    !finite(b.return_max)
  ) {
    warnings.push(
      "Return normalization is unknown for a version; Value and advantage magnitudes may not be comparable.",
    );
  } else if (a.return_min !== b.return_min || a.return_max !== b.return_max) {
    warnings.push(
      "Return ranges differ: normalized Value and advantage are not on the same original-return scale.",
    );
  }
  if (a.threshold !== b.threshold)
    warnings.push(
      "Thresholds differ; label flips can reflect the threshold as well as the model.",
    );
  if (a.lookahead !== b.lookahead)
    warnings.push(
      "Lookahead differs; continuous advantages use different future horizons.",
    );
  if (a.gamma !== b.gamma)
    warnings.push(
      "Discount factors differ; continuous advantages use different reward weighting.",
    );
  if (a.dataset_type !== b.dataset_type)
    warnings.push(
      "Dataset label rules differ; SFT labels may be forced positive.",
    );
  if (
    JSON.stringify(a.static_filter ?? null) !==
    JSON.stringify(b.static_filter ?? null)
  )
    warnings.push(
      "Static-filter coverage differs; metrics use only the frames labelled by both versions.",
    );
  if (a.stale || b.stale)
    warnings.push(
      "A selected result is stale; its data or outcome labels have changed since computation.",
    );
  return warnings;
}
