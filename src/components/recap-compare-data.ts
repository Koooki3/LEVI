/**
 * Data for the comparison charts of two saved RECAP results (pure, no React).
 *
 * The backend already aggregates (`by_outcome`, `distribution`); an older
 * backend without them gets the same shapes computed here from
 * `per_episode`, so the charts never need every frame. Every function caps
 * its output: a chart draws at most a few dozen bars whatever the dataset
 * size.
 */

import type {
  RecapComparison,
  RecapDistribution,
  RecapOutcomeGroup,
} from "@/types/recap.types";

const finite = (value: number | null | undefined): value is number =>
  typeof value === "number" && Number.isFinite(value);

const mean = (values: readonly number[]) =>
  values.length ? values.reduce((sum, v) => sum + v, 0) / values.length : NaN;

export const OUTCOME_ORDER = ["success", "failure", "unknown"] as const;

/** Catalog key of an outcome group. */
export function outcomeKey(outcome: string): string {
  switch (outcome) {
    case "success":
      return "Success episodes";
    case "failure":
      return "Failure episodes";
    default:
      return "Episodes without a shared outcome";
  }
}

/** The backend's groups, or the same groups computed from `per_episode`. */
export function outcomeGroups(data: RecapComparison): RecapOutcomeGroup[] {
  if (data.by_outcome) return data.by_outcome;
  const groups: RecapOutcomeGroup[] = [];
  for (const outcome of OUTCOME_ORDER) {
    const rows = data.per_episode.filter(
      (row) => (row.outcome ?? "unknown") === outcome,
    );
    if (!rows.length) continue;
    const frames = rows.reduce((sum, row) => sum + row.frames, 0);
    const weighted = (
      key: "label_agreement" | "positive_fraction_a" | "positive_fraction_b",
    ) =>
      frames && rows.every((row) => finite(row[key]))
        ? rows.reduce(
            (sum, row) => sum + (row[key] as number) * row.frames,
            0,
          ) / frames
        : null;
    groups.push({
      outcome,
      episodes: rows.length,
      frames,
      mean_value_a: mean(rows.map((row) => row.mean_value_a)),
      mean_value_b: mean(rows.map((row) => row.mean_value_b)),
      label_agreement: weighted("label_agreement"),
      positive_fraction_a: weighted("positive_fraction_a"),
      positive_fraction_b: weighted("positive_fraction_b"),
    });
  }
  return groups;
}

export interface AgreementBar {
  /** "all" or an outcome. */
  group: string;
  frames: number;
  /** Percentages, 0–100. */
  agreement: number;
  positiveA: number;
  positiveB: number;
}

/** Label agreement and positive-frame rates on all shared frames and per
 * outcome group; null when either result has no labels (values only). */
export function agreementBars(data: RecapComparison): AgreementBar[] | null {
  if (!data.labels) return null;
  const pct = (fraction: number) => fraction * 100;
  const bars: AgreementBar[] = [
    {
      group: "all",
      frames: data.frames.shared,
      agreement: pct(data.labels.agreement),
      positiveA: pct(data.labels.positive_fraction_a),
      positiveB: pct(data.labels.positive_fraction_b),
    },
  ];
  for (const group of outcomeGroups(data)) {
    if (
      !finite(group.label_agreement) ||
      !finite(group.positive_fraction_a) ||
      !finite(group.positive_fraction_b)
    )
      continue;
    bars.push({
      group: group.outcome,
      frames: group.frames,
      agreement: pct(group.label_agreement),
      positiveA: pct(group.positive_fraction_a),
      positiveB: pct(group.positive_fraction_b),
    });
  }
  return bars;
}

export interface OutcomeMeanBar {
  outcome: string;
  episodes: number;
  a: number;
  b: number;
  /** B − A */
  diff: number;
}

/** Mean V of A and B per outcome group (the means the AUC ranks). */
export function outcomeMeanBars(data: RecapComparison): OutcomeMeanBar[] {
  return outcomeGroups(data)
    .filter((g) => finite(g.mean_value_a) && finite(g.mean_value_b))
    .map((g) => ({
      outcome: g.outcome,
      episodes: g.episodes,
      a: g.mean_value_a,
      b: g.mean_value_b,
      diff: g.mean_value_b - g.mean_value_a,
    }));
}

export interface HistogramBar {
  lo: number;
  hi: number;
  a: number;
  b: number;
}

export const FALLBACK_BINS = 20;

/** Counts of `values` in `bins` equal bins over [lo, hi] (hi inclusive). */
export function binCounts(
  values: readonly number[],
  lo: number,
  hi: number,
  bins = FALLBACK_BINS,
): number[] {
  const counts = new Array<number>(bins).fill(0);
  const width = (hi - lo) / bins;
  if (!(width > 0)) return counts;
  for (const v of values) {
    if (!finite(v) || v < lo || v > hi) continue;
    counts[Math.min(bins - 1, Math.floor((v - lo) / width))] += 1;
  }
  return counts;
}

function edgesOf(lo: number, hi: number, bins: number): number[] {
  return Array.from(
    { length: bins + 1 },
    (_, i) => lo + ((hi - lo) * i) / bins,
  );
}

/** Value distributions of A and B on common bins. Frames when the backend
 * binned them (`distribution`), else the episode means (`per_episode`), and
 * `unit` says which. Null without data. */
export function valueHistogram(data: RecapComparison): {
  rows: HistogramBar[];
  unit: "frames" | "episodes";
} | null {
  const dist: RecapDistribution | null | undefined = data.distribution;
  if (dist && dist.value.edges.length === dist.value.a.length + 1) {
    const { edges, a, b } = dist.value;
    return {
      unit: "frames",
      rows: a.map((count, i) => ({
        lo: edges[i],
        hi: edges[i + 1],
        a: count,
        b: b[i] ?? 0,
      })),
    };
  }
  const a = data.per_episode.map((row) => row.mean_value_a).filter(finite);
  const b = data.per_episode.map((row) => row.mean_value_b).filter(finite);
  if (!a.length || !b.length) return null;
  let lo = Math.min(...a, ...b);
  let hi = Math.max(...a, ...b);
  if (hi - lo < 1e-6) {
    lo -= 0.005;
    hi += 0.005;
  }
  const edges = edgesOf(lo, hi, FALLBACK_BINS);
  const countsA = binCounts(a, lo, hi);
  const countsB = binCounts(b, lo, hi);
  return {
    unit: "episodes",
    rows: countsA.map((count, i) => ({
      lo: edges[i],
      hi: edges[i + 1],
      a: count,
      b: countsB[i],
    })),
  };
}

/** |B − A| per shared frame, binned by the backend; null without it. */
export function absDiffHistogram(
  data: RecapComparison,
): { lo: number; hi: number; count: number }[] | null {
  const dist = data.distribution?.abs_diff;
  if (!dist || dist.edges.length !== dist.counts.length + 1) return null;
  return dist.counts.map((count, i) => ({
    lo: dist.edges[i],
    hi: dist.edges[i + 1],
    count,
  }));
}

export interface EpisodeDiffBar {
  episode: number;
  outcome: string | null;
  /** Mean V of B minus mean V of A on the episode's shared frames. */
  diff: number;
  current: boolean;
}

export const EPISODE_BAR_LIMIT = 60;

/** Per-episode mean-V differences, largest first by sign. Past `limit`
 * episodes only the `limit` largest |B − A| are kept (the current episode is
 * always kept); `total` and `counts` describe every episode. */
export function episodeDiffBars(
  data: RecapComparison,
  currentEpisode: number | null,
  limit = EPISODE_BAR_LIMIT,
): {
  rows: EpisodeDiffBar[];
  total: number;
  /** Over every episode, not only the bars kept. */
  counts: { higherB: number; higherA: number; equal: number };
} {
  const all = data.per_episode
    .filter((row) => finite(row.mean_value_a) && finite(row.mean_value_b))
    .map((row) => ({
      episode: row.episode,
      outcome: row.outcome,
      diff: row.mean_value_b - row.mean_value_a,
      current: row.episode === currentEpisode,
    }));
  let kept = all;
  if (all.length > limit) {
    const byMagnitude = [...all].sort(
      (x, y) => Math.abs(y.diff) - Math.abs(x.diff) || x.episode - y.episode,
    );
    kept = byMagnitude.slice(0, limit);
    const current = all.find((row) => row.current);
    if (current && !kept.includes(current))
      kept = [...kept.slice(0, -1), current];
  }
  const counts = { higherB: 0, higherA: 0, equal: 0 };
  for (const row of all)
    if (row.diff > 0) counts.higherB += 1;
    else if (row.diff < 0) counts.higherA += 1;
    else counts.equal += 1;
  return {
    rows: [...kept].sort((x, y) => y.diff - x.diff || x.episode - y.episode),
    total: all.length,
    counts,
  };
}

/** A short number for chart labels. */
export function chartNumber(value: number, digits = 3): string {
  if (!finite(value)) return "—";
  const text = value.toFixed(digits);
  return /^-0\.?0*$/.test(text) ? text.slice(1) : text;
}
