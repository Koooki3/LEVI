import { describe, expect, test } from "bun:test";
import {
  AXIS_STORAGE_KEY,
  FIXED_VALUE_DOMAIN,
  MIN_AXIS_SPAN,
  MIN_STRENGTH,
  advantageRuns,
  datasetValueRange,
  diffDomain,
  formatTick,
  niceTicks,
  planValueAxis,
  readAxisMode,
  saveAxisMode,
  toReturnUnits,
  valueDiff,
  valueDomain,
  valueToPercent,
  formatSigned,
  framePeriod,
  labelledFrameAtTime,
  nearestFrame,
  positiveFraction,
  recapApplies,
  recapComparisonNoteKey,
  recapComparisonWarnings,
  recapSelection,
  recapView,
  thresholdSourceKey,
  valuePath,
  valuePaths,
  valueToY,
  type RecapViewInput,
} from "@/components/recap-lanes";
import type { RecapRevision, RecapSummary } from "@/types/recap.types";

const ts = (n: number, fps = 10) =>
  Array.from({ length: n }, (_, i) => i / fps);
const idx = (n: number) => Array.from({ length: n }, (_, i) => i);

const revision = (
  id: string,
  extra: Partial<RecapRevision> = {},
): RecapRevision => ({
  revision_id: id,
  checkpoint: "value-" + id,
  step: 3000,
  provider: "rlinf",
  created_at: 1790000000,
  episodes: 1,
  frames: 10,
  threshold: 0.005278945887678077,
  threshold_source: "checkpoint",
  positive_quantile: 0.3,
  lookahead: 10,
  positive_fraction: 0.3,
  stale: false,
  return_min: -799,
  return_max: 0,
  current: false,
  dataset_type: "rollout",
  gamma: 1,
  ...extra,
});

describe("saved result selection", () => {
  test("opens current and keeps a selected history when a new current run arrives", () => {
    const results = {
      current: "r2",
      revisions: [revision("r2"), revision("r1")],
    };
    expect(recapSelection(results, { primary: "", comparison: "" })).toEqual({
      primary: "r2",
      comparison: "",
    });
    expect(
      recapSelection(results, { primary: "r1", comparison: "r2" }),
    ).toEqual({ primary: "r1", comparison: "r2" });
  });
  test("empty results, vanished selections and identical sides do not invent versions", () => {
    expect(
      recapSelection(
        { current: null, revisions: [] },
        { primary: "r1", comparison: "r2" },
      ),
    ).toEqual({ primary: "", comparison: "" });
    const results = { current: "missing", revisions: [revision("new")] };
    expect(
      recapSelection(results, { primary: "r1", comparison: "new" }),
    ).toEqual({ primary: "new", comparison: "" });
  });
});

describe("comparison geometry and boundaries", () => {
  test("polylines and playhead readings never bridge static-filter gaps", () => {
    expect(
      valuePaths(
        [-1, -0.8, -0.4, 0],
        [0, 0.1, 0.5, 0.6],
        [0, 1, 5, 6],
        1,
        100,
        100,
      ),
    ).toEqual(["0,100 10,80", "50,40 60,0"]);
    expect(labelledFrameAtTime([0, 0.1, 0.5, 0.6], 10, 0.3)).toBe(-1);
    expect(labelledFrameAtTime([0, 0.1, 0.5, 0.6], 10, 0.12)).toBe(1);
  });
  test("frame-start intervals match advantage labels without borrowing a future retained frame", () => {
    const frames = [0, 1, 5, 6];
    const timestamps = [0, 0.1, 0.5, 0.6];
    const runs = advantageRuns(
      frames,
      [true, true, true, true],
      [1, 1, 1, 1],
      timestamps,
      10,
    );
    for (const [time, expectedFrame] of [
      [-0.01, null],
      [0, 0],
      [0.1, 1],
      [0.17, 1],
      [0.2, null],
      [0.46, null],
      [0.5, 5],
      [0.69, 6],
      [0.7, null],
    ] as const) {
      const index = labelledFrameAtTime(timestamps, 10, time);
      expect(index < 0 ? null : frames[index]).toBe(expectedFrame);
      expect(runs.some((run) => time >= run.start && time < run.end)).toBe(
        expectedFrame != null,
      );
    }
  });
  test("decimal frame ends remain excluded and unsorted payloads return the saved index", () => {
    expect(labelledFrameAtTime([0, 0.1, 0.2, 0.5], 10, 0.3)).toBe(-1);
    expect(labelledFrameAtTime([0, 0.1, 0.2, 0.5], 10, 0.299999)).toBe(2);
    expect(labelledFrameAtTime([0.5, Number.NaN, 0.1, 0.6], 10, 0.17)).toBe(2);
    expect(labelledFrameAtTime([0.5, Number.NaN, 0.1, 0.6], 10, 0.46)).toBe(-1);
    expect(labelledFrameAtTime([0, 0.25, 0.5], 0, 0.37)).toBe(1);
    expect(labelledFrameAtTime([0, 0.25, 0.5], 0, 0.75)).toBe(-1);
    expect(labelledFrameAtTime([0], 10, Number.NaN)).toBe(-1);
  });
  test("a non-finite sample breaks the plot and sorted timestamps preserve frame identity", () => {
    expect(
      valuePaths([-0.2, -1, Number.NaN], [0.2, 0, 0.1], [2, 0, 1], 1, 100, 100),
    ).toEqual(["0,100", "20,20"]);
    expect(valuePaths([0], [0], [0], 0, 100, 100)).toEqual([]);
    expect(labelledFrameAtTime([], 10, 0)).toBe(-1);
  });
  test("comparison warns on scale and threshold differences, including missing ranges", () => {
    const a = revision("r1", { return_min: -1000, threshold: 0.00749 });
    const b = revision("r2");
    const warnings = recapComparisonWarnings(a, b);
    expect(warnings).toContain(
      "Return ranges differ: normalized Value and advantage are not on the same original-return scale.",
    );
    expect(warnings).toContain(
      "Thresholds differ; label flips can reflect the threshold as well as the model.",
    );
    expect(recapComparisonWarnings(b, { ...b, return_min: null })).toContain(
      "Return normalization is unknown for a version; Value and advantage magnitudes may not be comparable.",
    );
    expect(recapComparisonWarnings(b, b)).toEqual([]);
    expect(recapComparisonNoteKey("fingerprint_unavailable")).toContain(
      "source fingerprint",
    );
    expect(recapComparisonNoteKey("future-note")).toContain(
      "additional computation differences",
    );
  });
});

describe("advantageRuns", () => {
  test("returns nothing for an empty episode", () => {
    expect(advantageRuns([], [], [], [], 10)).toEqual([]);
  });

  test("a single frame spans one frame period", () => {
    const runs = advantageRuns([0], [true], [0.2], [0], 10, 0);
    expect(runs).toHaveLength(1);
    expect(runs[0].start).toBe(0);
    expect(runs[0].end).toBeCloseTo(0.1);
    expect(runs[0].positive).toBe(true);
    expect(runs[0].meanAdv).toBeCloseTo(0.2);
    expect(runs[0].count).toBe(1);
    expect(runs[0].strength).toBe(1);
  });

  test("merges consecutive frames with the same label", () => {
    const positive = [true, true, false, false, false, true];
    const advantage = [0.1, 0.3, -0.2, -0.4, -0.6, 0.05];
    const runs = advantageRuns(idx(6), positive, advantage, ts(6), 10, 0);
    expect(runs.map((r) => [r.positive, r.count])).toEqual([
      [true, 2],
      [false, 3],
      [true, 1],
    ]);
    expect(runs[0].start).toBe(0);
    expect(runs[0].end).toBeCloseTo(0.2);
    // Adjacent runs touch: no gap, no overlap.
    expect(runs[1].start).toBeCloseTo(runs[0].end);
    expect(runs[2].start).toBeCloseTo(runs[1].end);
    expect(runs[2].end).toBeCloseTo(0.6);
    expect(runs[0].meanAdv).toBeCloseTo(0.2);
    expect(runs[1].meanAdv).toBeCloseTo(-0.4);
    expect(runs[1].firstFrame).toBe(2);
    expect(runs[1].lastFrame).toBe(4);
  });

  test("an all-positive episode is one run", () => {
    const runs = advantageRuns(
      idx(50),
      Array(50).fill(true),
      Array(50).fill(0.1),
      ts(50),
      10,
    );
    expect(runs).toHaveLength(1);
    expect(runs[0].end).toBeCloseTo(5);
  });

  test("strength is normalised per episode and clamped to MIN_STRENGTH..1", () => {
    const thr = -0.1;
    // Gaps from threshold: 0.01, 0.5, 1.0 (the last one is the scale).
    const advantage = [thr + 0.01, thr - 0.5, thr + 1.0];
    const runs = advantageRuns(
      idx(3),
      [true, false, true],
      advantage,
      ts(3),
      10,
      thr,
    );
    expect(runs[0].strength).toBe(MIN_STRENGTH);
    expect(runs[1].strength).toBeCloseTo(0.5);
    expect(runs[2].strength).toBe(1);
    for (const r of runs) {
      expect(r.strength).toBeGreaterThanOrEqual(MIN_STRENGTH);
      expect(r.strength).toBeLessThanOrEqual(1);
    }
  });

  test("a flat advantage equal to the threshold still draws at full strength", () => {
    const runs = advantageRuns(
      idx(4),
      [true, true, true, true],
      [0, 0, 0, 0],
      ts(4),
      10,
      0,
    );
    expect(runs[0].strength).toBe(1);
  });

  test("non-finite advantages and timestamps are skipped, not propagated", () => {
    const runs = advantageRuns(
      idx(5),
      [true, true, true, false, false],
      [0.2, Number.NaN, 0.4, Number.POSITIVE_INFINITY, Number.NaN],
      [0, 0.1, Number.NaN, 0.3, 0.4],
      10,
      0,
    );
    // Frame 2 has no timestamp: the frame-index gap splits the positive run.
    expect(runs.map((r) => [r.positive, r.count])).toEqual([
      [true, 2],
      [false, 2],
    ]);
    expect(runs[0].meanAdv).toBeCloseTo(0.2);
    expect(runs[1].meanAdv).toBeNull();
    expect(runs[1].strength).toBe(MIN_STRENGTH);
    for (const r of runs) {
      expect(Number.isFinite(r.start)).toBe(true);
      expect(Number.isFinite(r.end)).toBe(true);
    }
  });

  test("a gap in frame indices breaks a run", () => {
    const runs = advantageRuns(
      [0, 1, 5, 6],
      [true, true, true, true],
      [0.1, 0.1, 0.1, 0.1],
      [0, 0.1, 0.5, 0.6],
      10,
    );
    expect(runs).toHaveLength(2);
    expect(runs[0].end).toBeCloseTo(0.2);
    expect(runs[1].start).toBeCloseTo(0.5);
  });

  test("unsorted input is ordered by timestamp", () => {
    const runs = advantageRuns(
      [2, 0, 1],
      [false, true, true],
      [-0.5, 0.1, 0.2],
      [0.2, 0, 0.1],
      10,
    );
    expect(runs.map((r) => [r.positive, r.start])).toEqual([
      [true, 0],
      [false, 0.2],
    ]);
  });

  test("mismatched array lengths use the common prefix", () => {
    const runs = advantageRuns(idx(3), [true, true], [0.1], ts(5), 10);
    expect(runs).toHaveLength(1);
    expect(runs[0].count).toBe(2);
  });

  test("an invalid fps falls back to the timestamp step", () => {
    const runs = advantageRuns(
      idx(3),
      [true, true, true],
      [1, 1, 1],
      ts(3, 4),
      0,
    );
    expect(runs[0].end).toBeCloseTo(0.75);
  });
});

describe("framePeriod", () => {
  test("prefers fps, then the median step, then zero", () => {
    expect(framePeriod(20)).toBeCloseTo(0.05);
    expect(framePeriod(Number.NaN, [0, 0.1, 0.2, 0.9])).toBeCloseTo(0.1);
    expect(framePeriod(-1, [0])).toBe(0);
  });
});

describe("valuePath", () => {
  test("maps -1 to the bottom and 0 to the top", () => {
    expect(valuePath([0, -1, -0.5], [0, 5, 10], 10, 100, 20)).toBe(
      "0,0 50,20 100,10",
    );
  });

  test("clamps out-of-range values and times onto the box", () => {
    expect(valuePath([0.5, -3], [-1, 20], 10, 100, 20)).toBe("0,0 100,20");
  });

  test("skips non-finite values and returns '' when nothing is drawable", () => {
    expect(valuePath([Number.NaN, -0.5], [0, 10], 10, 100, 20)).toBe("100,10");
    expect(valuePath([], [], 10, 100, 20)).toBe("");
    expect(valuePath([-0.5], [0], 0, 100, 20)).toBe("");
    expect(valuePath([-0.5], [0], 10, 0, 20)).toBe("");
  });

  test("draws in time order even for unsorted input", () => {
    expect(valuePath([-1, 0], [10, 0], 10, 100, 20)).toBe("0,0 100,20");
  });

  test("valueToY is linear", () => {
    expect(valueToY(-0.25, 40)).toBe(10);
  });
});

describe("nearestFrame", () => {
  const times = [0, 0.1, 0.2, 0.3];

  test("finds the closest frame, ties to the earlier one", () => {
    expect(nearestFrame(times, 0.12)).toBe(1);
    expect(nearestFrame(times, 0.18)).toBe(2);
    expect(nearestFrame(times, 0.04)).toBe(0);
    expect(nearestFrame([0, 1, 2], 0.5)).toBe(0);
  });

  test("clamps to the first and last frames", () => {
    expect(nearestFrame(times, -3)).toBe(0);
    expect(nearestFrame(times, 99)).toBe(3);
  });

  test("returns -1 without frames or with a non-finite time", () => {
    expect(nearestFrame([], 1)).toBe(-1);
    expect(nearestFrame(times, Number.NaN)).toBe(-1);
  });

  test("falls back to a linear scan on unsorted or NaN timestamps", () => {
    expect(nearestFrame([0.3, 0, 0.2], 0.21)).toBe(2);
    expect(nearestFrame([0, Number.NaN, 0.2], 0.19)).toBe(2);
  });
});

describe("formatting", () => {
  test("positiveFraction", () => {
    expect(positiveFraction([])).toBeNull();
    expect(positiveFraction([true, false, false, true])).toBe(0.5);
  });

  test("formatSigned", () => {
    expect(formatSigned(0.01234)).toBe("+0.012");
    expect(formatSigned(-0.5, 2)).toBe("-0.50");
    expect(formatSigned(null)).toBe("—");
    expect(formatSigned(Number.NaN)).toBe("—");
  });
});

describe("recapView", () => {
  const base: RecapViewInput = {
    statusLoaded: true,
    statusError: false,
    hasCheckpoints: true,
    hasCurrent: true,
    jobActive: false,
    episode: "labels",
    controlsOpen: false,
  };

  test("a labelled episode shows rows, meta and a recompute affordance", () => {
    expect(recapView(base)).toEqual({
      note: null,
      control: "recompute",
      showMeta: true,
      showRows: true,
    });
    expect(recapView({ ...base, controlsOpen: true }).control).toBe("compute");
  });

  test("collapses to one muted line without checkpoints or a result", () => {
    expect(
      recapView({
        ...base,
        hasCheckpoints: false,
        hasCurrent: false,
        episode: "none",
      }),
    ).toEqual({
      note: "no-checkpoint",
      control: null,
      showMeta: false,
      showRows: false,
    });
  });

  test("offers compute when checkpoints exist but nothing was computed", () => {
    const view = recapView({ ...base, hasCurrent: false, episode: "none" });
    expect(view.note).toBeNull();
    expect(view.control).toBe("compute");
    expect(view.showRows).toBe(false);
  });

  test("an active job shows progress instead of controls", () => {
    const view = recapView({
      ...base,
      hasCurrent: false,
      episode: "none",
      jobActive: true,
    });
    expect(view.control).toBe("progress");
    expect(view.note).toBeNull();
    // Even with no checkpoints listed any more, a running job stays visible.
    expect(
      recapView({
        ...base,
        hasCheckpoints: false,
        hasCurrent: false,
        episode: "none",
        jobActive: true,
      }).note,
    ).toBeNull();
  });

  test("keeps empty rows while an episode loads under a result", () => {
    const view = recapView({ ...base, episode: "loading" });
    expect(view.showRows).toBe(true);
    expect(view.showMeta).toBe(false);
    expect(
      recapView({ ...base, hasCurrent: false, episode: "loading" }).showRows,
    ).toBe(false);
  });

  test("an episode outside the result says so", () => {
    expect(recapView({ ...base, episode: "none" }).note).toBe(
      "no-episode-labels",
    );
  });

  test("loading and unavailable status", () => {
    expect(
      recapView({ ...base, statusLoaded: false, episode: "loading" }).note,
    ).toBe("loading");
    const failed = recapView({
      ...base,
      statusLoaded: false,
      statusError: true,
      episode: "none",
    });
    expect(failed.note).toBe("unavailable");
    expect(failed.control).toBeNull();
    // Labels that did load still draw, even if status failed.
    expect(
      recapView({ ...base, statusLoaded: false, statusError: true }).showRows,
    ).toBe(true);
  });
});

describe("thresholdSourceKey / recapApplies", () => {
  test("names every threshold source and passes unknown ones through", () => {
    expect(thresholdSourceKey("manual")).toBe(
      "threshold set by hand for this run",
    );
    expect(thresholdSourceKey("checkpoint")).toBe(
      "the checkpoint's unified threshold",
    );
    expect(thresholdSourceKey("dataset_quantile")).toBe(
      "quantile of this dataset's advantages",
    );
    expect(thresholdSourceKey("other")).toBe("other");
    expect(thresholdSourceKey(null)).toBe("");
  });

  test("labels exist only for registered local datasets", () => {
    expect(recapApplies("local/plates--ns")).toBe(true);
    expect(recapApplies("lerobot/pusht")).toBe(false);
    expect(recapApplies(null)).toBe(false);
  });
});

describe("valueDomain", () => {
  test("fixed is always the model's [-1, 0], whatever the data", () => {
    expect(valueDomain([[-0.2, -0.1]], { mode: "fixed" })).toEqual({
      lo: -1,
      hi: 0,
    });
  });

  test("adaptive zooms in with headroom around a small spread", () => {
    const d = valueDomain([[-0.16, -0.05]], { mode: "adaptive" });
    expect(d.lo).toBeLessThan(-0.16);
    expect(d.hi).toBeGreaterThan(-0.05);
    // a span of ~0.11 plus 8 % padding each side, far tighter than [-1, 0]
    expect(d.hi - d.lo).toBeLessThan(0.15);
    expect(d.hi - d.lo).toBeGreaterThan(0.11);
  });

  test("a constant curve gets the minimum span, centred, not a zero range", () => {
    const d = valueDomain([[-0.5, -0.5, -0.5]], { mode: "adaptive" });
    expect(d.hi - d.lo).toBeGreaterThanOrEqual(MIN_AXIS_SPAN);
    expect((d.lo + d.hi) / 2).toBeCloseTo(-0.5, 6);
    expect(valueToY(-0.5, 100, d)).toBeCloseTo(50, 3);
  });

  test("a near-constant curve is not stretched into apparent swings", () => {
    const d = valueDomain([[-0.5, -0.5001, -0.4999]], { mode: "adaptive" });
    expect(d.hi - d.lo).toBeGreaterThanOrEqual(MIN_AXIS_SPAN);
  });

  test("never leaves the model's support by more than the margin; slides instead of squeezing", () => {
    const flatAtZero = valueDomain([[0, 0]], { mode: "adaptive" });
    expect(flatAtZero.hi).toBeCloseTo(0.02, 9);
    expect(flatAtZero.hi - flatAtZero.lo).toBeGreaterThanOrEqual(MIN_AXIS_SPAN);
    const flatAtFloor = valueDomain([[-1, -1]], { mode: "adaptive" });
    expect(flatAtFloor.lo).toBeCloseTo(-1.02, 9);
    expect(flatAtFloor.hi - flatAtFloor.lo).toBeGreaterThanOrEqual(
      MIN_AXIS_SPAN,
    );
  });

  test("ignores NaN and infinities; no data falls back to the support", () => {
    const d = valueDomain([[Number.NaN, -0.3, Infinity, -0.2]], {
      mode: "adaptive",
    });
    expect(d.lo).toBeLessThan(-0.3);
    expect(d.hi).toBeGreaterThan(-0.2);
    expect(d.hi - d.lo).toBeLessThan(0.3);
    expect(
      valueDomain([[Number.NaN], null, undefined], { mode: "adaptive" }),
    ).toEqual({ lo: -1.02, hi: 0.02 });
  });

  test("values outside the support never widen past it", () => {
    const d = valueDomain([[-3, 2]], { mode: "adaptive" });
    expect(d).toEqual({ lo: -1.02, hi: 0.02 });
  });

  test("two results share one axis covering both", () => {
    const d = valueDomain(
      [
        [-0.9, -0.8],
        [-0.2, -0.1],
      ],
      { mode: "adaptive" },
    );
    expect(d.lo).toBeLessThan(-0.9);
    expect(d.hi).toBeGreaterThan(-0.1);
  });

  test("a model with a narrower support bounds the axis", () => {
    const d = valueDomain([[-0.5, -0.4]], {
      mode: "adaptive",
      support: { lo: -0.5, hi: -0.2 },
    });
    expect(d.lo).toBeGreaterThanOrEqual(-0.52 - 1e-9);
    expect(d.hi).toBeLessThanOrEqual(-0.18 + 1e-9);
  });

  test("dataset range holds still across episodes and still shows an outlier", () => {
    const range = { lo: -0.9, hi: -0.1 };
    const quiet = valueDomain([[-0.5, -0.4]], {
      mode: "dataset",
      datasetRange: range,
    });
    const other = valueDomain([[-0.7, -0.2]], {
      mode: "dataset",
      datasetRange: range,
    });
    expect(quiet).toEqual(other);
    const outlier = valueDomain([[-0.99, -0.4]], {
      mode: "dataset",
      datasetRange: range,
    });
    expect(outlier.lo).toBeLessThan(-0.99);
  });

  test("dataset range without a range behaves as adaptive", () => {
    expect(valueDomain([[-0.5, -0.4]], { mode: "dataset" })).toEqual(
      valueDomain([[-0.5, -0.4]], { mode: "adaptive" }),
    );
  });
});

describe("valueToY with a domain", () => {
  test("keeps the old fixed behaviour by default and clamps to the box", () => {
    expect(valueToY(-0.25, 40)).toBe(10);
    expect(valueToY(-5, 40, { lo: -0.2, hi: -0.1 })).toBe(40);
    expect(valueToY(5, 40, { lo: -0.2, hi: -0.1 })).toBe(0);
  });

  test("a narrow domain magnifies a small swing", () => {
    const narrow = { lo: -0.2, hi: -0.1 };
    expect(valueToY(-0.15, 100, narrow)).toBeCloseTo(50, 6);
    expect(valueToPercent(-0.2, narrow)).toBe(100);
  });

  test("a degenerate domain draws mid-height instead of dividing by zero", () => {
    expect(valueToY(-0.5, 40, { lo: -0.5, hi: -0.5 })).toBe(20);
  });

  test("valuePaths/valuePath take the domain and break at gaps", () => {
    const d = { lo: -0.2, hi: -0.1 };
    expect(valuePath([-0.1, -0.2], [0, 10], 10, 100, 20, d)).toBe("0,0 100,20");
    const paths = valuePaths(
      [-0.1, -0.15, -0.2],
      [0, 5, 10],
      [0, 1, 3],
      10,
      100,
      20,
      d,
    );
    expect(paths).toEqual(["0,0 50,10", "100,20"]);
    expect(FIXED_VALUE_DOMAIN).toEqual({ lo: -1, hi: 0 });
  });
});

describe("niceTicks and formatTick", () => {
  test("round ticks inside the domain with matching decimals", () => {
    const { ticks, step } = niceTicks({ lo: -0.17, hi: -0.04 }, 4);
    expect(ticks.length).toBeGreaterThanOrEqual(2);
    for (const t of ticks) {
      expect(t).toBeGreaterThanOrEqual(-0.17);
      expect(t).toBeLessThanOrEqual(-0.04);
      expect(Math.abs(t / step - Math.round(t / step))).toBeLessThan(1e-9);
    }
    expect(ticks.map((t) => formatTick(t, step))).toEqual(
      ticks.map((t) => (t === 0 ? "0" : t.toFixed(2).replace("-", "−"))),
    );
  });

  test("the fixed range gets 0, -0.5 and -1 style ticks", () => {
    const { ticks, step } = niceTicks({ lo: -1, hi: 0 }, 3);
    expect(ticks).toEqual([-1, -0.5, 0]);
    expect(formatTick(-0.5, step)).toBe("−0.5");
    expect(formatTick(0, step)).toBe("0");
    expect(formatTick(-1, step)).toBe("−1.0");
  });

  test("a domain that fits no coarse tick steps down through 1/2/5 values", () => {
    // 0.5 would leave one tick inside [-0.964, -0.036]; 0.25 would not be a 1/2/5 step
    const { ticks, step } = niceTicks({ lo: -0.964, hi: -0.036 }, 3);
    expect(step).toBeCloseTo(0.2, 9);
    expect(ticks).toEqual([-0.8, -0.6, -0.4, -0.2]);
  });

  test("tiny and huge spans stay clean", () => {
    const tiny = niceTicks({ lo: -0.5025, hi: -0.4975 }, 3);
    expect(tiny.ticks.length).toBeGreaterThanOrEqual(2);
    expect(tiny.ticks.every((t) => Number.isFinite(t))).toBe(true);
    const big = niceTicks({ lo: -799, hi: 12 }, 4);
    expect(big.ticks.every((t) => Number.isInteger(t))).toBe(true);
    expect(formatTick(-200, big.step)).toBe("−200");
  });

  test("degenerate input yields no ticks and a dash for NaN", () => {
    expect(niceTicks({ lo: 1, hi: 1 }).ticks).toEqual([]);
    expect(niceTicks({ lo: 0, hi: 1 }, 1).ticks).toEqual([]);
    expect(formatTick(Number.NaN)).toBe("—");
    expect(formatTick(-0.00001, 0.05)).toBe("0");
  });
});

describe("toReturnUnits", () => {
  test("is the inverse of the return normalization and keeps NaN", () => {
    expect(toReturnUnits([-1, 0, -0.5], -800, 0)).toEqual([-800, 0, -400]);
    expect(toReturnUnits([Number.NaN, 0], -10, 10)[0]).toBeNaN();
    expect(toReturnUnits([-0.5], null, 0)[0]).toBeNaN();
  });
});

describe("planValueAxis", () => {
  const rev = (min: number | null, max: number | null, bins = true) => ({
    return_min: min,
    return_max: max,
    value_support: bins ? { v_min: -1, v_max: 0 } : null,
  });
  const a = [-0.4, -0.3, -0.2];
  const b = [-0.8, -0.6, -0.4];

  test("one shared axis for both results in normalized units", () => {
    const plan = planValueAxis({
      mode: "adaptive",
      a,
      b,
      revA: rev(-799, 0),
      revB: rev(-1000, 0),
    });
    expect(plan.units).toBe("normalized");
    expect(plan.domain.lo).toBeLessThan(-0.8);
    expect(plan.domain.hi).toBeGreaterThan(-0.2);
    expect(plan.refs).toEqual([0, -1]);
    expect(plan.b).toEqual(b);
  });

  test("return units puts different return ranges on one scale", () => {
    const plan = planValueAxis({
      mode: "return",
      a: [0, 0],
      b: [0, 0],
      revA: rev(-799, 0),
      revB: rev(-1000, 0),
    });
    expect(plan.mode).toBe("return");
    expect(plan.units).toBe("return");
    expect(plan.a).toEqual([0, 0]);
    // V=-1 is -799 on A's scale and -1000 on B's
    const low = planValueAxis({
      mode: "return",
      a: [-1],
      b: [-1],
      revA: rev(-799, 0),
      revB: rev(-1000, 0),
    });
    expect(low.a).toEqual([-799]);
    expect(low.b).toEqual([-1000]);
    expect(low.domain.lo).toBeLessThan(-1000);
    expect(low.refs).toEqual([0]);
  });

  test("return units unavailable without a return range: falls back to adaptive with a reason", () => {
    const plan = planValueAxis({
      mode: "return",
      a,
      revA: rev(null, null),
    });
    expect(plan.mode).toBe("adaptive");
    expect(plan.unavailable.return).toContain("Return units");
    const oneSide = planValueAxis({
      mode: "return",
      a,
      b,
      revA: rev(-799, 0),
      revB: rev(null, 0),
    });
    expect(oneSide.mode).toBe("adaptive");
    expect(oneSide.unavailable.return).toBeDefined();
    // equal bounds would divide the axis by nothing
    expect(
      planValueAxis({ mode: "return", a, revA: rev(0, 0) }).unavailable.return,
    ).toBeDefined();
  });

  test("dataset range needs a range for every shown result", () => {
    const base = { mode: "dataset" as const, a, revA: rev(-1, 0) };
    const none = planValueAxis(base);
    expect(none.mode).toBe("adaptive");
    expect(none.unavailable.dataset).toContain("Dataset range");
    const ok = planValueAxis({
      ...base,
      datasetA: { lo: -0.9, hi: -0.1 },
    });
    expect(ok.mode).toBe("dataset");
    expect(ok.unavailable.dataset).toBeUndefined();
    const bMissing = planValueAxis({
      ...base,
      b,
      revB: rev(-1, 0),
      datasetA: { lo: -0.9, hi: -0.1 },
    });
    expect(bMissing.mode).toBe("adaptive");
  });

  test("a different value support shapes the axis limits", () => {
    const plan = planValueAxis({
      mode: "adaptive",
      a: [-0.5],
      revA: {
        return_min: -1,
        return_max: 0,
        value_support: { v_min: -0.6, v_max: -0.4 },
      },
    });
    expect(plan.domain.lo).toBeGreaterThanOrEqual(-0.62 - 1e-9);
    expect(plan.domain.hi).toBeLessThanOrEqual(-0.38 + 1e-9);
  });

  test("fixed ignores the data; missing episodes draw an empty but valid axis", () => {
    expect(planValueAxis({ mode: "fixed", a }).domain).toEqual({
      lo: -1,
      hi: 0,
    });
    const empty = planValueAxis({ mode: "adaptive", a: undefined });
    expect(empty.a).toEqual([]);
    expect(empty.domain.hi).toBeGreaterThan(empty.domain.lo);
  });
});

describe("datasetValueRange", () => {
  const summary = (
    rows: Array<[number | undefined, number | undefined]>,
  ): RecapSummary =>
    ({
      revision_id: "r",
      threshold: 0,
      episodes: Object.fromEntries(
        rows.map(([lo, hi], i) => [
          String(i),
          {
            positive_fraction: 0.5,
            mean_advantage: 0,
            mean_value: -0.5,
            frames: 10,
            min_value: lo,
            max_value: hi,
          },
        ]),
      ),
    }) as unknown as RecapSummary;

  test("null for old results, nothing, or no finite pair", () => {
    expect(datasetValueRange(null)).toBeNull();
    expect(datasetValueRange(summary([[undefined, undefined]]))).toBeNull();
    expect(datasetValueRange(summary([[Number.NaN, -0.1]]))).toBeNull();
    expect(
      datasetValueRange({ revision_id: "r", threshold: 0 } as RecapSummary),
    ).toBeNull();
  });

  test("a single outlier episode cannot stretch the range (p1 / p99)", () => {
    const rows: Array<[number, number]> = Array.from({ length: 200 }, () => [
      -0.6, -0.2,
    ]);
    rows[0] = [-1, 0];
    expect(datasetValueRange(summary(rows))).toEqual({ lo: -0.6, hi: -0.2 });
  });

  test("small datasets use their extremes", () => {
    expect(
      datasetValueRange(
        summary([
          [-0.8, -0.3],
          [-0.5, -0.1],
        ]),
      ),
    ).toEqual({ lo: -0.8, hi: -0.1 });
  });
});

describe("valueDiff and diffDomain", () => {
  test("B minus A on shared frames; frames one result lacks leave a gap", () => {
    const d = valueDiff(
      {
        frame_index: [0, 1, 2, 4],
        timestamp: [0, 0.1, 0.2, 0.4],
        value: [-0.5, -0.4, Number.NaN, -0.2],
      },
      {
        frame_index: [0, 1, 2, 3, 4],
        timestamp: [0, 0.1, 0.2, 0.3, 0.4],
        value: [-0.4, -0.4, -0.3, -0.3, -0.5],
      },
    );
    expect(d.frame_index).toEqual([0, 1, 4]);
    expect(d.timestamp).toEqual([0, 0.1, 0.4]);
    expect(d.value[0]).toBeCloseTo(0.1);
    expect(d.value[1]).toBeCloseTo(0);
    expect(d.value[2]).toBeCloseTo(-0.3);
    // frame 1 -> frame 4 is not consecutive, so the line breaks there
    expect(
      valuePaths(d.value, d.timestamp, d.frame_index, 0.4, 100, 20, {
        lo: -1,
        hi: 1,
      }),
    ).toHaveLength(2);
  });

  test("disjoint or empty inputs give an empty series", () => {
    const none = valueDiff(
      { frame_index: [0], timestamp: [0], value: [1] },
      { frame_index: [5], timestamp: [0], value: [1] },
    );
    expect(none.value).toEqual([]);
    expect(
      valueDiff(
        { frame_index: [], timestamp: [], value: [] },
        { frame_index: [], timestamp: [], value: [] },
      ).value,
    ).toEqual([]);
  });

  test("the difference axis is symmetric about zero", () => {
    const d = diffDomain([0.1, -0.3, 0.05], "adaptive");
    expect(d.lo).toBe(-d.hi);
    expect(d.hi).toBeGreaterThan(0.3);
    expect(valueToY(0, 100, d)).toBeCloseTo(50, 9);
    expect(diffDomain([], "adaptive").hi).toBeGreaterThan(0);
    expect(diffDomain([0, 0], "adaptive").hi).toBeGreaterThanOrEqual(
      MIN_AXIS_SPAN / 2,
    );
    expect(diffDomain([0.1], "fixed")).toEqual({ lo: -1, hi: 1 });
  });
});

describe("remembered axis mode", () => {
  const store = (initial?: string | null) => {
    const data = new Map<string, string>();
    if (initial != null) data.set(AXIS_STORAGE_KEY, initial);
    return {
      getItem: (k: string) => data.get(k) ?? null,
      setItem: (k: string, v: string) => void data.set(k, v),
    };
  };

  test("round-trips, ignores junk, and survives a storage that throws", () => {
    const s = store();
    expect(readAxisMode(s)).toBe("adaptive");
    expect(saveAxisMode("fixed", s)).toBe(true);
    expect(readAxisMode(s)).toBe("fixed");
    expect(readAxisMode(store("logarithmic"))).toBe("adaptive");
    expect(readAxisMode(null)).toBe("adaptive");
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("quota");
      },
    };
    expect(readAxisMode(broken)).toBe("adaptive");
    expect(saveAxisMode("return", broken)).toBe(false);
    expect(saveAxisMode("return", null)).toBe(false);
  });
});
