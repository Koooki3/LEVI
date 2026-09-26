import { describe, expect, test } from "bun:test";
import {
  MIN_STRENGTH,
  advantageRuns,
  formatSigned,
  framePeriod,
  nearestFrame,
  positiveFraction,
  recapView,
  valuePath,
  valueToY,
  type RecapViewInput,
} from "@/components/recap-lanes";

const ts = (n: number, fps = 10) =>
  Array.from({ length: n }, (_, i) => i / fps);
const idx = (n: number) => Array.from({ length: n }, (_, i) => i);

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

  test("strength is normalised per episode and clamped to 0.25..1", () => {
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
