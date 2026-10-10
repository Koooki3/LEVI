/**
 * Pure geometry for the timeline's VALUE MODEL section (RECAP advantage
 * labels). No React, no DOM — everything here is unit-tested.
 *
 * - `advantageRuns` merges consecutive frames with the same positive/negative
 *   label into segments the ADVANTAGE row draws.
 * - `valuePath` maps V(o_t) onto an SVG polyline for the VALUE row; the
 *   vertical range is a `ValueDomain` (`valueDomain` picks it: adaptive,
 *   dataset range, fixed [-1, 0] or return units; `valueDiff` builds B − A).
 * - `nearestFrame` finds the frame under the playhead for the value readout.
 *
 * The backend returns frames sorted by timestamp; the helpers still tolerate
 * unsorted input, mismatched array lengths and non-finite numbers so a bad
 * payload degrades to fewer marks rather than a broken timeline.
 */

import type {
  RecapEpisodeSummary,
  RecapRevision,
  RecapRevisions,
  RecapSummary,
} from "@/types/recap.types";

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

/** A vertical range of the VALUE row; `lo < hi`. */
export interface ValueDomain {
  lo: number;
  hi: number;
}

/** What the VALUE row showed before the axis became selectable. */
export const FIXED_VALUE_DOMAIN: ValueDomain = { lo: -1, hi: 0 };

/** Map a value to a y coordinate: the domain's top at 0, its bottom at
 * `height`. Out-of-range values are clamped onto the plot. The default domain
 * is the normalized V range [-1, 0]. */
export function valueToY(
  value: number,
  height: number,
  domain: ValueDomain = FIXED_VALUE_DOMAIN,
): number {
  const span = domain.hi - domain.lo;
  if (!(span > 0)) return height / 2;
  const v = Math.max(domain.lo, Math.min(domain.hi, value));
  return ((domain.hi - v) / span) * height;
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
  domain: ValueDomain = FIXED_VALUE_DOMAIN,
): string {
  if (!(duration > 0) || !(width > 0) || !(height > 0)) return "";
  const order = frameOrder(timestamps, values.length);
  const points: string[] = [];
  for (const i of order) {
    const v = values[i];
    if (!finite(v)) continue;
    const x = Math.max(0, Math.min(width, (timestamps[i] / duration) * width));
    points.push(`${round(x)},${round(valueToY(v, height, domain))}`);
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
  domain: ValueDomain = FIXED_VALUE_DOMAIN,
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
    points.push(`${round(x)},${round(valueToY(v, height, domain))}`);
    previous = frame;
  }
  close();
  return paths;
}

// ---- Vertical axis -----------------------------------------------------

/** `adaptive` zooms to the shown curves, `dataset` to the dataset's spread
 * (stable while the episode changes), `fixed` is the model's [-1, 0], and
 * `return` is adaptive in the original return units. */
export type AxisMode = "adaptive" | "dataset" | "fixed" | "return";
export const AXIS_MODES: readonly AxisMode[] = [
  "adaptive",
  "dataset",
  "fixed",
  "return",
];
export const DEFAULT_AXIS_MODE: AxisMode = "adaptive";
export const AXIS_STORAGE_KEY = "levi.recap.valueAxis";

/** Narrowest span the adaptive axis draws: below it a flat curve would turn
 * quantisation noise into apparent swings. */
export const MIN_AXIS_SPAN = 0.05;
/** Headroom around the data, as a share of its span, and its absolute floor. */
const AXIS_PAD_FRACTION = 0.08;
const AXIS_PAD_FLOOR = 0.01;
/** The axis may extend this far past the model's support (v_min, v_max). */
const SUPPORT_MARGIN = 0.02;

export const isAxisMode = (value: unknown): value is AxisMode =>
  typeof value === "string" &&
  (AXIS_MODES as readonly string[]).includes(value);

/** The remembered mode, or the default when storage is empty, blocked or
 * holds something else. Never throws. */
export function readAxisMode(
  storage?: Pick<Storage, "getItem"> | null,
): AxisMode {
  try {
    const raw = storage?.getItem(AXIS_STORAGE_KEY);
    return isAxisMode(raw) ? raw : DEFAULT_AXIS_MODE;
  } catch {
    return DEFAULT_AXIS_MODE;
  }
}

/** Remember a mode as a per-person preference; false when storage refused. */
export function saveAxisMode(
  mode: AxisMode,
  storage?: Pick<Storage, "setItem"> | null,
): boolean {
  try {
    if (!storage) return false;
    storage.setItem(AXIS_STORAGE_KEY, mode);
    return true;
  } catch {
    return false;
  }
}

/** Finite [min, max] over every series, or null when nothing is finite. */
export function seriesExtent(
  series: ReadonlyArray<readonly number[] | null | undefined>,
): ValueDomain | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const values of series)
    for (const v of values ?? []) {
      if (!finite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  return lo <= hi ? { lo, hi } : null;
}

/** Pad a data range, widen it to the minimum span around its centre, and move
 * it (keeping its span where it can) inside `bounds`. */
function framed(extent: ValueDomain, bounds: ValueDomain | null): ValueDomain {
  let { lo, hi } = extent;
  if (hi - lo < MIN_AXIS_SPAN) {
    const centre = (lo + hi) / 2;
    lo = centre - MIN_AXIS_SPAN / 2;
    hi = centre + MIN_AXIS_SPAN / 2;
  }
  const pad = Math.max((hi - lo) * AXIS_PAD_FRACTION, AXIS_PAD_FLOOR);
  lo -= pad;
  hi += pad;
  if (bounds) {
    if (hi - lo >= bounds.hi - bounds.lo) return { ...bounds };
    if (lo < bounds.lo) {
      hi += bounds.lo - lo;
      lo = bounds.lo;
    }
    if (hi > bounds.hi) {
      lo -= hi - bounds.hi;
      hi = bounds.hi;
    }
  }
  return { lo, hi };
}

export interface ValueDomainOptions {
  mode: AxisMode;
  /** The model's value support (v_min, v_max); [-1, 0] when unknown. The axis
   * stays within it plus a small margin. */
  support?: ValueDomain | null;
  /** Dataset-wide range for `dataset` mode; without it that mode behaves as
   * `adaptive`. */
  datasetRange?: ValueDomain | null;
}

/** The vertical range for the curves to be drawn (A and, when comparing, B on
 * one shared axis). `return` mode expects series already in return units and
 * `support` in the same units; it is the adaptive rule. */
export function valueDomain(
  series: ReadonlyArray<readonly number[] | null | undefined>,
  options: ValueDomainOptions,
): ValueDomain {
  const support = options.support ?? FIXED_VALUE_DOMAIN;
  if (options.mode === "fixed") return { ...FIXED_VALUE_DOMAIN };
  const bounds: ValueDomain = {
    lo: support.lo - SUPPORT_MARGIN,
    hi: support.hi + SUPPORT_MARGIN,
  };
  const own = seriesExtent(series);
  const wide =
    options.mode === "dataset" && options.datasetRange
      ? own
        ? {
            lo: Math.min(own.lo, options.datasetRange.lo),
            hi: Math.max(own.hi, options.datasetRange.hi),
          }
        : options.datasetRange
      : own;
  return wide ? framed(wide, bounds) : { ...bounds };
}

/** Percentile (0..1, nearest rank) of a list; NaN when empty. */
function percentile(values: number[], q: number): number {
  if (!values.length) return Number.NaN;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
}

/** The spread of a dataset's curves, robust to a few outlier episodes: the 1st
 * percentile of the episode minima to the 99th of the maxima. Reads the
 * optional `min_value`/`max_value` a result stores for each episode; null for
 * results saved without them. */
export function datasetValueRange(
  summary: RecapSummary | null | undefined,
): ValueDomain | null {
  if (!summary?.episodes) return null;
  const mins: number[] = [];
  const maxs: number[] = [];
  for (const row of Object.values(summary.episodes) as Array<
    RecapEpisodeSummary & { min_value?: unknown; max_value?: unknown }
  >) {
    if (finite(row?.min_value) && finite(row?.max_value)) {
      mins.push(row.min_value);
      maxs.push(row.max_value);
    }
  }
  if (!mins.length) return null;
  const lo = percentile(mins, 0.01);
  const hi = percentile(maxs, 0.99);
  return lo <= hi ? { lo, hi } : null;
}

/** Normalized V -> original return: the inverse of the model's return
 * normalization, `(V + 1)(max − min) + min` (the form `compare` uses for its
 * return-unit statistics). Non-finite input stays non-finite. */
export function toReturnUnits(
  values: readonly number[],
  returnMin: number | null | undefined,
  returnMax: number | null | undefined,
): number[] {
  if (!finite(returnMin) || !finite(returnMax)) return values.map(() => NaN);
  return values.map((v) =>
    finite(v) ? (v + 1) * (returnMax - returnMin) + returnMin : NaN,
  );
}

type AxisRevision = Pick<
  RecapRevision,
  "return_min" | "return_max" | "value_support"
>;

export interface AxisInputs {
  /** The person's choice. */
  mode: AxisMode;
  a: readonly number[] | null | undefined;
  /** Null unless result B is shown beside A. */
  b?: readonly number[] | null;
  revA?: AxisRevision | null;
  revB?: AxisRevision | null;
  datasetA?: ValueDomain | null;
  datasetB?: ValueDomain | null;
}

export interface AxisPlan {
  /** The mode in effect: the choice, or `adaptive` when it is unavailable. */
  mode: AxisMode;
  /** Catalogue key explaining why a mode cannot be used. */
  unavailable: Partial<Record<AxisMode, string>>;
  domain: ValueDomain;
  /** The curves in the axis' units (normalized V, or original returns). */
  a: number[];
  b: number[] | null;
  units: "normalized" | "return";
  /** Values that get a solid reference line when inside the domain. */
  refs: number[];
}

const returnSpan = (rev: AxisRevision | null | undefined) =>
  rev &&
  finite(rev.return_min) &&
  finite(rev.return_max) &&
  rev.return_max > rev.return_min
    ? { min: rev.return_min, max: rev.return_max }
    : null;

function supportOf(rev: AxisRevision | null | undefined): ValueDomain {
  const lo = rev?.value_support?.v_min;
  const hi = rev?.value_support?.v_max;
  return finite(lo) && finite(hi) && lo < hi
    ? { lo, hi }
    : { ...FIXED_VALUE_DOMAIN };
}

/** One shared vertical axis for A and (when shown) B, with the reason a mode
 * is unavailable. Two results with different return ranges still share an
 * axis: only `return` mode puts them in common units. */
export function planValueAxis(input: AxisInputs): AxisPlan {
  const comparing = input.b != null;
  const unavailable: Partial<Record<AxisMode, string>> = {};
  if (!input.datasetA || (comparing && !input.datasetB))
    unavailable.dataset =
      "Dataset range needs per-episode minima and maxima, which this result does not store.";
  const spanA = returnSpan(input.revA);
  const spanB = returnSpan(input.revB);
  if (!spanA || (comparing && !spanB))
    unavailable.return =
      "Return units need the return range of every shown result.";
  const mode = unavailable[input.mode] ? DEFAULT_AXIS_MODE : input.mode;

  const supportA = supportOf(input.revA);
  const supportB = comparing ? supportOf(input.revB) : null;
  const union = (x: ValueDomain, y: ValueDomain | null): ValueDomain =>
    y ? { lo: Math.min(x.lo, y.lo), hi: Math.max(x.hi, y.hi) } : x;

  if (mode === "return" && spanA) {
    const a = toReturnUnits(input.a ?? [], spanA.min, spanA.max);
    const b =
      comparing && spanB
        ? toReturnUnits(input.b ?? [], spanB.min, spanB.max)
        : null;
    const edges = (
      support: ValueDomain,
      span: { min: number; max: number },
    ) => {
      const [lo, hi] = toReturnUnits(
        [support.lo, support.hi],
        span.min,
        span.max,
      );
      return { lo, hi };
    };
    const support = union(
      edges(supportA, spanA),
      supportB && spanB ? edges(supportB, spanB) : null,
    );
    return {
      mode,
      unavailable,
      domain: valueDomain([a, b], { mode, support }),
      a,
      b,
      units: "return",
      refs: [0],
    };
  }

  const a = [...(input.a ?? [])];
  const b = comparing ? [...(input.b ?? [])] : null;
  const dataset =
    mode === "dataset" && input.datasetA
      ? union(input.datasetA, comparing ? (input.datasetB ?? null) : null)
      : null;
  return {
    mode,
    unavailable,
    domain: valueDomain([a, b], {
      mode,
      support: union(supportA, supportB),
      datasetRange: dataset,
    }),
    a,
    b,
    units: "normalized",
    refs: [0, -1],
  };
}

/** Round, evenly spaced tick values inside the domain (1, 2 or 5 × 10^k
 * steps), ascending; at most about `maxCount`, at least two when the domain
 * allows. */
export function niceTicks(
  domain: ValueDomain,
  maxCount = 4,
): { ticks: number[]; step: number } {
  const span = domain.hi - domain.lo;
  if (!(span > 0) || !(maxCount >= 2)) return { ticks: [], step: 0 };
  const stepFor = (raw: number) => {
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    for (const m of [1, 2, 5, 10])
      if (m * magnitude >= raw) return m * magnitude;
    return 10 * magnitude;
  };
  let step = stepFor(span / (maxCount - 1));
  const collect = (s: number) => {
    const out: number[] = [];
    const first = Math.ceil(domain.lo / s - 1e-9);
    for (let k = first; k * s <= domain.hi + s * 1e-9; k++)
      out.push(Number((k * s).toPrecision(12)));
    return out;
  };
  let ticks = collect(step);
  // Too coarse to place two ticks: take the next smaller 1/2/5 step.
  for (let tries = 0; tries < 3 && ticks.length < 2; tries++) {
    const magnitude = 10 ** Math.floor(Math.log10(step * 1.0001));
    const lead = Math.round(step / magnitude);
    step = lead === 5 ? 2 * magnitude : lead === 2 ? magnitude : magnitude / 2;
    ticks = collect(step);
  }
  return { ticks, step };
}

/** A tick or reading with just enough decimals for the step, using a true
 * minus sign. */
export function formatTick(value: number, step = 0): string {
  if (!finite(value)) return "—";
  const decimals =
    step > 0
      ? Math.min(6, Math.max(0, -Math.floor(Math.log10(step) + 1e-9)))
      : 3;
  const text =
    Math.abs(value) < 10 ** -(decimals + 1) ? "0" : value.toFixed(decimals);
  return text.startsWith("-") ? "−" + text.slice(1) : text;
}

/** Position of a value within a domain as a percentage from the top (clamped
 * to 0..100): drives the HTML tick labels and the playhead dot. */
export function valueToPercent(value: number, domain: ValueDomain): number {
  return valueToY(value, 100, domain);
}

// ---- Difference curve ----------------------------------------------------

export interface FrameSeries {
  frame_index: readonly number[];
  timestamp: readonly number[];
  value: readonly number[];
}

/** B − A on the frames both results label. Frames present in only one result,
 * or with a non-finite value, are left out, so `valuePaths` breaks the line at
 * a static-filter gap. Timestamps are A's. */
export function valueDiff(a: FrameSeries, b: FrameSeries) {
  const bAt = new Map<number, number>();
  const n = Math.min(b.frame_index.length, b.value.length);
  for (let i = 0; i < n; i++)
    if (finite(b.frame_index[i]) && finite(b.value[i]))
      bAt.set(b.frame_index[i], b.value[i]);
  const frames: number[] = [];
  const timestamps: number[] = [];
  const values: number[] = [];
  const m = Math.min(a.frame_index.length, a.value.length, a.timestamp.length);
  for (let i = 0; i < m; i++) {
    const frame = a.frame_index[i];
    const other = bAt.get(frame);
    if (!finite(frame) || !finite(a.value[i]) || other == null) continue;
    frames.push(frame);
    timestamps.push(a.timestamp[i]);
    values.push(other - a.value[i]);
  }
  return { frame_index: frames, timestamp: timestamps, value: values };
}

/** Axis for a difference curve: symmetric around 0 so the zero line is the
 * middle. `fixed` uses ±1 (the widest possible difference of normalized V). */
export function diffDomain(
  values: readonly number[],
  mode: AxisMode,
): ValueDomain {
  if (mode === "fixed") return { lo: -1, hi: 1 };
  const extent = seriesExtent([values]);
  const reach = extent ? Math.max(Math.abs(extent.lo), Math.abs(extent.hi)) : 0;
  const half = Math.max(reach * (1 + AXIS_PAD_FRACTION), MIN_AXIS_SPAN / 2);
  return { lo: -half, hi: half };
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
