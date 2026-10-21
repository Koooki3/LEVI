// Video time -> replay row, as pure functions (design:
// lab/levi-hub/programs/levi2/REPLAY-3D-DESIGN-20261021.md §3.2).
//
// Rows are found by their own `timestamp`, never by `frame / fps`:
// `flatChartData` keeps at most MAX_EPISODE_POINTS rows (evenly sampled,
// `fetch-data.ts`), so on a long episode row i is not frame i. Timestamps are
// episode-relative seconds; a segmented (v3) video reports file time, which
// `videoToEpisodeTime` turns into episode time the same way the player does.

/**
 * - `linear`: joint angles and positions.
 * - `step`: the row at or before t (gripper open/close commands, quaternions).
 * - `angle`: unwrapped Euler angles; linear, except that two neighbours more
 *   than π apart (a ±π wrap the unwrap missed) are never blended.
 */
export type SampleKind = "linear" | "step" | "angle";

export interface ReplayTrack {
  /** Episode-relative row timestamps in seconds, non-decreasing. */
  t: ArrayLike<number>;
  /** Row-major values: `t.length` rows of `width` numbers. */
  values: ArrayLike<number>;
  width: number;
}

/**
 * Index of the last row whose timestamp is <= t (with equal timestamps, the
 * last of them). Clamps: t before the first row gives 0, t after the last
 * gives the last row, NaN gives 0. An empty list gives -1.
 */
export function indexAt(t: number, timestamps: ArrayLike<number>): number {
  const n = timestamps.length;
  if (n === 0) return -1;
  if (!(t >= timestamps[0])) return 0;
  if (t >= timestamps[n - 1]) return n - 1;
  // Invariant: timestamps[lo] <= t < timestamps[hi].
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >>> 1;
    if (timestamps[mid] <= t) lo = mid;
    else hi = mid;
  }
  return lo;
}

/**
 * Values at time t, one per column. `kind` is one kind for every column or
 * one per column (e.g. x, y, z linear and rx, ry, rz angle). A NaN neighbour
 * is not blended: the earlier row's value is returned.
 */
export function sampleAt(
  t: number,
  track: ReplayTrack,
  kind: SampleKind | readonly SampleKind[],
  out: Float64Array = new Float64Array(track.width),
): Float64Array {
  const { t: ts, values, width } = track;
  if (typeof kind !== "string" && kind.length !== width) {
    throw new RangeError(`sampleAt: ${kind.length} kinds for ${width} columns`);
  }
  const n = ts.length;
  if (n === 0) return out.fill(NaN);
  const i = indexAt(t, ts);
  const blend = i < n - 1 && t > ts[i];
  const fraction = blend ? (t - ts[i]) / (ts[i + 1] - ts[i]) : 0;
  const base = i * width;
  for (let c = 0; c < width; c++) {
    const a = values[base + c];
    const columnKind = typeof kind === "string" ? kind : kind[c];
    if (!blend || columnKind === "step") {
      out[c] = a;
      continue;
    }
    const b = values[base + width + c];
    const wraps = columnKind === "angle" && Math.abs(b - a) > Math.PI;
    const keep = wraps || !Number.isFinite(a) || !Number.isFinite(b);
    out[c] = keep ? a : a + (b - a) * fraction;
  }
  return out;
}

/**
 * Packs `flatChartData` rows into typed arrays once, at load time. Rows
 * without a finite timestamp are dropped; a missing value is NaN; rows are
 * put in timestamp order if they are not already.
 */
export function packTrack(
  rows: readonly Record<string, number>[],
  cols: readonly string[],
  timestampKey = "timestamp",
): { t: Float64Array; values: Float64Array; width: number } {
  const kept = rows.filter((row) => Number.isFinite(row[timestampKey]));
  const sorted = kept.every(
    (row, i) => i === 0 || kept[i - 1][timestampKey] <= row[timestampKey],
  );
  const ordered = sorted
    ? kept
    : [...kept].sort((a, b) => a[timestampKey] - b[timestampKey]);
  const width = cols.length;
  const t = new Float64Array(ordered.length);
  const values = new Float64Array(ordered.length * width);
  ordered.forEach((row, i) => {
    t[i] = row[timestampKey];
    cols.forEach((col, c) => {
      const value = row[col];
      values[i * width + c] = typeof value === "number" ? value : NaN;
    });
  });
  return { t, values, width };
}

/**
 * Episode time of a video element's currentTime. Segmented (v3) videos hold
 * several episodes in one file; the player reports `currentTime -
 * segmentStart` (`simple-videos-player.tsx`, timeupdate handler), and row
 * timestamps are relative to the same start.
 */
export function videoToEpisodeTime(
  currentTime: number,
  segmentStart?: number | null,
): number {
  return currentTime - (segmentStart ?? 0);
}

/**
 * Where playback should be now, between two `timeupdate` events (about four
 * a second): the last reported time plus the wall time since, scaled by the
 * playback rate. Time never runs backwards (a negative or non-finite elapsed
 * time counts as 0) and a rate that is not a positive number holds still.
 */
export function nextTimeEstimate(
  lastT: number,
  lastWallMs: number,
  nowMs: number,
  playbackRate: number,
): number {
  const elapsedMs = nowMs - lastWallMs;
  const seconds =
    Number.isFinite(elapsedMs) && elapsedMs > 0 ? elapsedMs / 1000 : 0;
  const rate =
    Number.isFinite(playbackRate) && playbackRate > 0 ? playbackRate : 0;
  return lastT + seconds * rate;
}
