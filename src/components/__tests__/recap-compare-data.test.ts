import { describe, expect, test } from "bun:test";
import type { RecapComparison, RecapRevision } from "@/types/recap.types";
import {
  EPISODE_BAR_LIMIT,
  absDiffHistogram,
  agreementBars,
  binCounts,
  chartNumber,
  episodeDiffBars,
  outcomeGroups,
  outcomeMeanBars,
  valueHistogram,
} from "@/components/recap-compare-data";
import {
  datasetTypeKey,
  datasetTypeSourceKey,
  recapComparisonNoteKey,
  recapResultRows,
  recapValuesOnly,
} from "@/components/recap-lanes";

const side = {} as RecapRevision;
const row = (
  episode: number,
  outcome: string | null,
  a: number,
  b: number,
  more: Partial<RecapComparison["per_episode"][number]> = {},
) => ({
  episode,
  outcome,
  frames: 10,
  label_agreement: 0.5,
  positive_fraction_a: 0.2,
  positive_fraction_b: 0.4,
  mean_value_a: a,
  mean_value_b: b,
  value_mean_abs_diff: Math.abs(a - b),
  value_corr: null,
  ...more,
});
const comparison = (
  rows: RecapComparison["per_episode"],
  more: Partial<RecapComparison> = {},
): RecapComparison => ({
  dataset: "d",
  a: side,
  b: side,
  episodes: { shared: rows.length, only_a: 0, only_b: 0 },
  frames: { shared: rows.length * 10, only_a: 0, only_b: 0 },
  labels: {
    agreement: 0.5,
    positive_a_only: 1,
    positive_b_only: 2,
    both_positive: 3,
    both_negative: 4,
    positive_fraction_a: 0.2,
    positive_fraction_b: 0.4,
  },
  value: null,
  advantage: null,
  per_episode: rows,
  notes: [],
  outcome_separation: null,
  ...more,
});

describe("comparison chart data", () => {
  test("outcome groups fall back to per-episode rows on an older backend", () => {
    const data = comparison([
      row(0, "success", -0.2, -0.1),
      row(1, "success", -0.4, -0.3, { frames: 30, label_agreement: 1 }),
      row(2, "failure", -0.8, -0.6),
      row(3, null, -0.5, -0.5),
    ]);
    const groups = outcomeGroups(data);
    expect(groups.map((g) => g.outcome)).toEqual([
      "success",
      "failure",
      "unknown",
    ]);
    expect(groups[0].mean_value_a).toBeCloseTo(-0.3);
    // Frame-weighted: (0.5·10 + 1·30) / 40
    expect(groups[0].label_agreement).toBeCloseTo(0.875);
    const means = outcomeMeanBars(data);
    expect(means[1]).toMatchObject({ outcome: "failure", episodes: 1 });
    expect(means[1].diff).toBeCloseTo(0.2);
    // The backend's groups win when present.
    const given = [{ ...groups[0], episodes: 99 }];
    expect(outcomeGroups(comparison([], { by_outcome: given }))).toBe(given);
  });

  test("agreement bars are percentages, and absent for a values-only side", () => {
    const data = comparison([row(0, "success", -0.2, -0.1)]);
    const bars = agreementBars(data)!;
    expect(bars[0]).toMatchObject({
      group: "all",
      agreement: 50,
      positiveA: 20,
      positiveB: 40,
    });
    expect(bars[1].group).toBe("success");
    expect(agreementBars({ ...data, labels: null })).toBeNull();
    // Groups without label rates are left out, not drawn as zero.
    const unlabelled = comparison(
      [row(0, "success", -0.2, -0.1, { label_agreement: null })],
      {},
    );
    expect(agreementBars(unlabelled)!.map((b) => b.group)).toEqual(["all"]);
  });

  test("histograms use the backend bins, else bin the episode means", () => {
    const binned = comparison([row(0, "success", -0.2, -0.1)], {
      distribution: {
        bins: 2,
        value: { edges: [-1, -0.5, 0], a: [3, 4], b: [5, 2] },
        abs_diff: { edges: [0, 0.1, 0.2], counts: [6, 1] },
      },
    });
    expect(valueHistogram(binned)).toEqual({
      unit: "frames",
      rows: [
        { lo: -1, hi: -0.5, a: 3, b: 5 },
        { lo: -0.5, hi: 0, a: 4, b: 2 },
      ],
    });
    expect(absDiffHistogram(binned)).toEqual([
      { lo: 0, hi: 0.1, count: 6 },
      { lo: 0.1, hi: 0.2, count: 1 },
    ]);
    const fallback = valueHistogram(
      comparison([row(0, null, -0.5, -0.5), row(1, null, -0.5, -0.5)]),
    )!;
    expect(fallback.unit).toBe("episodes");
    // A constant still gets a non-empty range and every value is counted.
    expect(fallback.rows.reduce((s, r) => s + r.a, 0)).toBe(2);
    expect(fallback.rows.at(-1)!.hi).toBeGreaterThan(fallback.rows[0].lo);
    expect(absDiffHistogram(comparison([]))).toBeNull();
    expect(valueHistogram(comparison([]))).toBeNull();
  });

  test("bin counts include the upper edge and skip non-finite or outside values", () => {
    expect(binCounts([0, 0.5, 1, NaN, 2, -1], 0, 1, 2)).toEqual([1, 2]);
    expect(binCounts([1, 2], 1, 1, 3)).toEqual([0, 0, 0]);
  });

  test("per-episode bars are capped to the largest differences, keeping the current episode", () => {
    const rows = Array.from({ length: 200 }, (_, i) =>
      row(i, null, -0.5, -0.5 + (i % 2 ? 1 : -1) * i * 0.001),
    );
    const { rows: bars, total } = episodeDiffBars(comparison(rows), 3);
    expect(total).toBe(200);
    expect(bars).toHaveLength(EPISODE_BAR_LIMIT);
    expect(bars.some((b) => b.episode === 3 && b.current)).toBe(true);
    expect(bars.some((b) => b.episode === 199)).toBe(true);
    // Sorted by signed difference, B-higher first.
    for (let i = 1; i < bars.length; i += 1)
      expect(bars[i - 1].diff).toBeGreaterThanOrEqual(bars[i].diff);
    const small = episodeDiffBars(comparison(rows.slice(0, 5)), null);
    expect(small.rows).toHaveLength(5);
  });

  test("chart numbers never print a negative zero", () => {
    expect(chartNumber(-0.0001, 2)).toBe("0.00");
    expect(chartNumber(-0.25, 2)).toBe("-0.25");
    expect(chartNumber(NaN)).toBe("—");
  });
});

const result = (
  id: string,
  checkpoint: string,
  created: number,
  more: Partial<RecapRevision> = {},
) =>
  ({
    revision_id: id,
    checkpoint,
    created_at: created,
    current: false,
    ...more,
  }) as RecapRevision;

describe("result rows", () => {
  test("model results stay one per model; per-run results fold to the newest run of each checkpoint", () => {
    const list = {
      current: "m1",
      revisions: [
        result("m1", "m1", 50, { layout: "models", model: "m1" }),
        result("20261010-0900", "m2", 40),
        result("20261009-0900", "m1", 30),
        result("20261008-0900", "m2", 20),
        result("20261007-0900", "m3", 10, { layout: "revisions" }),
      ],
    };
    const folded = recapResultRows(list);
    expect(folded.rows.map((r) => r.revision_id)).toEqual([
      "m1",
      "20261010-0900",
      "20261007-0900",
    ]);
    expect(folded.hidden).toBe(2);
    const all = recapResultRows(list, { showEarlier: true });
    expect(all.rows).toHaveLength(5);
    expect(all.hidden).toBe(0);
    const kept = recapResultRows(list, { keep: ["20261008-0900"] });
    expect(kept.rows.map((r) => r.revision_id)).toContain("20261008-0900");
    expect(kept.hidden).toBe(1);
  });

  test("values-only results and label-rule wording", () => {
    expect(recapValuesOnly({ labels: false })).toBe(true);
    expect(recapValuesOnly({ dataset_type: "value_only" })).toBe(true);
    expect(recapValuesOnly({ labels: true, dataset_type: "rollout" })).toBe(
      false,
    );
    expect(recapValuesOnly(null)).toBe(false);
    expect(datasetTypeKey("sft")).toBe("Demonstrations (SFT)");
    expect(datasetTypeKey("value_only")).toBe(
      "Values only (no success/failure labels)",
    );
    expect(datasetTypeSourceKey("fallback")).toContain("fell back");
    expect(recapComparisonNoteKey("labels_unavailable")).toContain(
      "values only",
    );
  });
});
