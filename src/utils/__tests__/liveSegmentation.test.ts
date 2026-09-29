import { describe, expect, test } from "bun:test";
import {
  chooseResult,
  createCoalescer,
  decodeRle,
  displayedFrame,
  insertBounded,
  parseEpisodeList,
} from "@/utils/liveSegmentation";

describe("decodeRle", () => {
  test("decodes column-major COCO counts into a row-major mask", () => {
    // 2 rows x 3 columns; column-major order: (0,0) (1,0) (0,1) (1,1) (0,2) (1,2)
    // counts: 1 background, 2 foreground, 3 background → pixels (1,0) and (0,1).
    const mask = decodeRle({ size: [2, 3], counts: [1, 2, 3] });
    expect(mask.width).toBe(3);
    expect(mask.height).toBe(2);
    expect([...mask.data]).toEqual([0, 1, 0, 1, 0, 0]);
  });

  test("ignores counts past the image", () => {
    const mask = decodeRle({ size: [1, 2], counts: [0, 5] });
    expect([...mask.data]).toEqual([1, 1]);
  });
});

describe("frame choice", () => {
  test("displayed frame is episode-local and robust to float error", () => {
    expect(displayedFrame(1.0, 30)).toBe(30);
    expect(displayedFrame(0.1 * 3, 10)).toBe(3);
    expect(displayedFrame(12.5, 10, 10)).toBe(25);
    expect(displayedFrame(5, 10, 10)).toBe(0);
  });

  test("uses the exact frame, else a recent earlier one, never a stale one", () => {
    const frames = new Map([
      [10, "f10"],
      [14, "f14"],
    ]);
    expect(chooseResult(frames, 14)).toBe("f14");
    expect(chooseResult(frames, 13)).toBe("f10");
    expect(chooseResult(frames, 16)).toBe("f14");
    expect(chooseResult(frames, 17)).toBe("f14");
    expect(chooseResult(frames, 18)).toBeNull();
    // A later frame is never shown early.
    expect(chooseResult(frames, 9)).toBeNull();
    expect(chooseResult(frames, 13, 2)).toBeNull();
    expect(chooseResult(undefined, 3)).toBeNull();
  });

  test("bounded store keeps the newest frames", () => {
    const frames = new Map<number, number>();
    for (let i = 0; i < 10; i += 1) insertBounded(frames, i, i, 4);
    expect([...frames.keys()].sort((a, b) => a - b)).toEqual([6, 7, 8, 9]);
  });
});

describe("parseEpisodeList", () => {
  test("parses ranges and single episodes", () => {
    expect(parseEpisodeList("0-3, 7")).toEqual([0, 1, 2, 3, 7]);
    expect(parseEpisodeList(" 5 - 6 ; 2,2 ")).toEqual([2, 5, 6]);
    expect(parseEpisodeList("")).toEqual([]);
  });

  test("rejects malformed input", () => {
    expect(() => parseEpisodeList("4-2")).toThrow();
    expect(() => parseEpisodeList("a")).toThrow();
    expect(() => parseEpisodeList("-1")).toThrow();
  });
});

describe("createCoalescer", () => {
  test("keeps one call in flight and sends only the latest value", async () => {
    const sent: number[] = [];
    const releases: (() => void)[] = [];
    const send = createCoalescer((value: number) => {
      sent.push(value);
      return new Promise<void>((resolve) => releases.push(resolve));
    });
    send(1);
    send(2);
    send(3);
    expect(sent).toEqual([1]);
    releases.shift()?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sent).toEqual([1, 3]);
    releases.shift()?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sent).toEqual([1, 3]);
  });

  test("a failed call does not block the next", async () => {
    const sent: number[] = [];
    const send = createCoalescer(async (value: number) => {
      sent.push(value);
      throw new Error("offline");
    });
    send(1);
    send(2);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sent).toEqual([1, 2]);
  });
});
