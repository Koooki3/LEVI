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

export interface AdvantageRun {
  /** Seconds: timestamp of the run's first frame. */
  start: number;
  /** Seconds: last frame's timestamp + one frame period. */
  end: number;
  positive: boolean;
  /** Mean continuous advantage over the run's finite values; null if none. */
  meanAdv: number | null;
  /** 0.25..1 — mean |A − threshold| of the run over the episode's scale. */
  strength: number;
  firstFrame: number;
  lastFrame: number;
  /** Number of frames merged into this run. */
  count: number;
}

export const MIN_STRENGTH = 0.25;

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
