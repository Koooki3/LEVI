import { describe, expect, test } from "bun:test";
import {
  indexAt,
  nextTimeEstimate,
  packTrack,
  sampleAt,
  videoToEpisodeTime,
  type ReplayTrack,
} from "@/lib/replay/clock";

const track = (t: number[], rows: number[][]): ReplayTrack => ({
  t,
  values: rows.flat(),
  width: rows[0]?.length ?? 0,
});

describe("indexAt", () => {
  const ts = [0, 0.5, 1, 1.5, 2];

  test("exact hits and points between rows give the row at or before t", () => {
    expect(indexAt(0, ts)).toBe(0);
    expect(indexAt(1, ts)).toBe(2);
    expect(indexAt(1.49, ts)).toBe(2);
    expect(indexAt(1.5, ts)).toBe(3);
    expect(indexAt(2, ts)).toBe(4);
  });

  test("clamps before the first and after the last row", () => {
    expect(indexAt(-3, ts)).toBe(0);
    expect(indexAt(-Infinity, ts)).toBe(0);
    expect(indexAt(99, ts)).toBe(4);
    expect(indexAt(Infinity, ts)).toBe(4);
    expect(indexAt(Number.NaN, ts)).toBe(0);
    // A list that does not start at 0 clamps too.
    expect(indexAt(0.2, [1, 2, 3])).toBe(0);
  });

  test("empty and single-row lists", () => {
    expect(indexAt(1, [])).toBe(-1);
    expect(indexAt(-1, [5])).toBe(0);
    expect(indexAt(9, [5])).toBe(0);
  });

  test("equal timestamps give the last of them", () => {
    expect(indexAt(1, [0, 1, 1, 1, 2])).toBe(3);
    expect(indexAt(1.5, [0, 1, 1, 1, 2])).toBe(3);
    expect(indexAt(0, [0, 0, 1])).toBe(1);
    expect(indexAt(2, [0, 2, 2])).toBe(2);
  });

  test("uses each row's timestamp, not a uniform or frame/fps spacing", () => {
    // Non-uniform rows: a uniform spacing guess (t / last * (n - 1)) gives
    // row 1, frame/fps at 10 fps gives frame 10 (clamped to 4).
    const uneven = [0, 0.1, 0.5, 2.0, 2.1];
    expect(indexAt(1.0, uneven)).toBe(2);
    expect(indexAt(0.3, uneven)).toBe(1);
    expect(indexAt(2.05, uneven)).toBe(3);
  });

  test("a 9000-frame episode sampled to 4000 rows still maps time correctly", () => {
    const fps = 30;
    const frames = 9000;
    const kept = 4000;
    // Evenly spaced picks, as fetch-data.ts evenlySampleArray does.
    const ts = Array.from(
      { length: kept },
      (_, i) => Math.round((i * (frames - 1)) / (kept - 1)) / fps,
    );
    const t = 150;
    const i = indexAt(t, ts);
    expect(ts[i]).toBeLessThanOrEqual(t);
    expect(ts[i + 1]).toBeGreaterThan(t);
    // Within one sampled step (about 2.25 frames) of the true frame.
    expect(t - ts[i]).toBeLessThan(3 / fps);
    // Row t * fps (= 4500) would be past the end of the sampled rows.
    expect(i).toBeLessThan(t * fps);
  });
});

describe("sampleAt", () => {
  test("joints are linearly interpolated", () => {
    const joints = track([0, 1], [
      [0, 1, -2],
      [1, 3, 2],
    ]);
    const out = sampleAt(0.25, joints, "linear");
    expect(out[0]).toBeCloseTo(0.25, 12);
    expect(out[1]).toBeCloseTo(1.5, 12);
    expect(out[2]).toBeCloseTo(-1, 12);
  });

  test("gripper is a step: the row at or before t, never a blend", () => {
    const gripper = track([0, 1, 2], [[1], [0], [1]]);
    expect(sampleAt(0.5, gripper, "step")[0]).toBe(1);
    expect(sampleAt(0.999, gripper, "step")[0]).toBe(1);
    expect(sampleAt(1, gripper, "step")[0]).toBe(0);
    expect(sampleAt(1.5, gripper, "step")[0]).toBe(0);
  });

  test("angles blend normally but never across a ±π jump", () => {
    const near = track([0, 1], [[0.2], [0.6]]);
    expect(sampleAt(0.5, near, "angle")[0]).toBeCloseTo(0.4, 12);
    // Unwrapped angles past π are fine to blend while they stay close.
    const unwrapped = track([0, 1], [[3.0], [3.3]]);
    expect(sampleAt(0.5, unwrapped, "angle")[0]).toBeCloseTo(3.15, 12);
    // 3.1 -> -3.1 is a wrap: blending would pass through 0 (the wrong way).
    const wrap = track([0, 1], [[3.1], [-3.1]]);
    expect(sampleAt(0.5, wrap, "angle")[0]).toBe(3.1);
    expect(sampleAt(0.99, wrap, "angle")[0]).toBe(3.1);
    expect(sampleAt(1, wrap, "angle")[0]).toBe(-3.1);
    const back = track([0, 1], [[-3.1], [3.1]]);
    expect(sampleAt(0.5, back, "angle")[0]).toBe(-3.1);
  });

  test("one kind per column: x linear, rz angle, gripper step", () => {
    const pose = track([0, 2], [
      [0, 3.1, 1],
      [2, -3.1, 0],
    ]);
    const out = sampleAt(1, pose, ["linear", "angle", "step"]);
    expect(out[0]).toBeCloseTo(1, 12);
    expect(out[1]).toBe(3.1);
    expect(out[2]).toBe(1);
  });

  test("a kinds list of the wrong length is an error", () => {
    const pose = track([0, 1], [
      [0, 0],
      [1, 1],
    ]);
    expect(() => sampleAt(0.5, pose, ["linear"])).toThrow(RangeError);
  });

  test("clamps at both ends and does not blend with a NaN neighbour", () => {
    const joints = track([1, 2], [[10], [20]]);
    expect(sampleAt(0, joints, "linear")[0]).toBe(10);
    expect(sampleAt(5, joints, "linear")[0]).toBe(20);
    const gap = track([0, 1], [[4], [Number.NaN]]);
    expect(sampleAt(0.5, gap, "linear")[0]).toBe(4);
  });

  test("an empty track gives NaN; out is reused", () => {
    const empty: ReplayTrack = { t: [], values: [], width: 2 };
    const out = sampleAt(0, empty, "linear");
    expect(Number.isNaN(out[0])).toBe(true);
    expect(Number.isNaN(out[1])).toBe(true);
    const buffer = new Float64Array(1);
    const joints = track([0, 1], [[0], [2]]);
    expect(sampleAt(0.5, joints, "linear", buffer)).toBe(buffer);
    expect(buffer[0]).toBeCloseTo(1, 12);
  });
});

describe("packTrack", () => {
  test("packs columns in order, NaN for a missing value", () => {
    const packed = packTrack(
      [
        { timestamp: 0, "observation.state | joint_0": 1 },
        {
          timestamp: 0.1,
          "observation.state | joint_0": 2,
          "observation.state | joint_1": 5,
        },
      ],
      ["observation.state | joint_0", "observation.state | joint_1"],
    );
    expect([...packed.t]).toEqual([0, 0.1]);
    expect(packed.width).toBe(2);
    expect(packed.values[0]).toBe(1);
    expect(Number.isNaN(packed.values[1])).toBe(true);
    expect([...packed.values.slice(2)]).toEqual([2, 5]);
  });

  test("drops rows without a finite timestamp and sorts unordered rows", () => {
    const packed = packTrack(
      [
        { timestamp: 2, a: 20 },
        { timestamp: Number.NaN, a: 99 },
        { timestamp: 1, a: 10 },
        { a: 98 },
      ],
      ["a"],
    );
    expect([...packed.t]).toEqual([1, 2]);
    expect([...packed.values]).toEqual([10, 20]);
  });
});

describe("segmented (v3) video time", () => {
  test("rows are episode-relative; the video side subtracts segmentStart", () => {
    // v3: one file holds several episodes; this one occupies 12.5..14.5 s.
    // fetch-data.ts (getEpisodeDataV3 chart rows) spreads the rows over the
    // segment duration starting at 0, not at video_from_timestamp.
    const video_from_timestamp = 12.5;
    const video_to_timestamp = 14.5;
    const n = 5;
    const duration = video_to_timestamp - video_from_timestamp;
    const ts = Array.from(
      { length: n },
      (_, index) => (index / Math.max(n - 1, 1)) * duration,
    );
    expect(ts).toEqual([0, 0.5, 1, 1.5, 2]);
    expect(Math.max(...ts)).toBeLessThan(video_from_timestamp);

    const currentTime = 13.5;
    const t = videoToEpisodeTime(currentTime, video_from_timestamp);
    expect(t).toBeCloseTo(1, 12);
    expect(indexAt(t, ts)).toBe(2);
    // Without the offset the file time would clamp to the last row.
    expect(indexAt(currentTime, ts)).toBe(n - 1);
    expect(videoToEpisodeTime(12.5, 12.5)).toBe(0);
  });

  test("an unsegmented video passes its time through", () => {
    expect(videoToEpisodeTime(3.25, 0)).toBe(3.25);
    expect(videoToEpisodeTime(3.25, null)).toBe(3.25);
    expect(videoToEpisodeTime(3.25)).toBe(3.25);
  });
});

describe("nextTimeEstimate", () => {
  test("extrapolates by wall time at playbackRate 1 and 2", () => {
    expect(nextTimeEstimate(10, 1000, 1250, 1)).toBeCloseTo(10.25, 12);
    expect(nextTimeEstimate(10, 1000, 1250, 2)).toBeCloseTo(10.5, 12);
    expect(nextTimeEstimate(10, 1000, 1000, 2)).toBe(10);
    expect(nextTimeEstimate(0, 0, 33, 0.5)).toBeCloseTo(0.0165, 12);
  });

  test("never runs backwards and holds still on a bad rate or clock", () => {
    expect(nextTimeEstimate(10, 1000, 900, 1)).toBe(10);
    expect(nextTimeEstimate(10, 1000, 1250, 0)).toBe(10);
    expect(nextTimeEstimate(10, 1000, 1250, -1)).toBe(10);
    expect(nextTimeEstimate(10, 1000, 1250, Number.NaN)).toBe(10);
    expect(nextTimeEstimate(10, Number.NaN, 1250, 1)).toBe(10);
  });
});
