import { describe, expect, test } from "bun:test";
import {
  meanAxisDomain,
  meanLabelDigits,
  wrapTickLines,
} from "@/components/recap-compare-charts";

describe("wrapTickLines", () => {
  test("keeps a name that fits on one line", () => {
    expect(wrapTickLines("Failure episodes", 200)).toEqual([
      "Failure episodes",
    ]);
  });

  test("breaks at spaces so neighbouring names fit their own band", () => {
    expect(wrapTickLines("Success episodes", 90)).toEqual([
      "Success",
      "episodes",
    ]);
    expect(wrapTickLines("All shared frames", 60)).toEqual([
      "All",
      "shared",
      "frames",
    ]);
  });

  test("breaks inside a word or a run of Chinese that is wider than the band", () => {
    const lines = wrapTickLines("成功片段成功片段成功片段", 50);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines.join("").replace("…", "")).toBe(
      "成功片段成功片段成功片段".slice(
        0,
        lines.join("").replace("…", "").length,
      ),
    );
    for (const line of lines) expect([...line].length).toBeLessThanOrEqual(4);
  });

  test("never returns more than three lines", () => {
    const lines = wrapTickLines(
      "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      30,
    );
    expect(lines).toHaveLength(3);
    expect(lines[2].endsWith("…")).toBe(true);
  });

  test("an empty name still yields one (empty) line", () => {
    expect(wrapTickLines("", 80)).toEqual([""]);
  });
});

describe("meanAxisDomain", () => {
  test("leaves room below the longest negative bar for its label", () => {
    const [lo, hi] = meanAxisDomain([-0.235, -0.07, -0.166, -0.758], 120);
    expect(hi).toBe(0);
    expect(lo).toBeLessThan(-0.758);
    // About one label height (16px of 120px plot) beyond the bar.
    const pixels = ((lo - -0.758) / lo) * 120;
    expect(Math.abs(pixels)).toBeGreaterThanOrEqual(14);
  });

  test("leaves room above positive bars and handles an all-zero chart", () => {
    const [lo, hi] = meanAxisDomain([0.2, -0.3], 120);
    expect(lo).toBeLessThan(-0.3);
    expect(hi).toBeGreaterThan(0.2);
    expect(meanAxisDomain([0, 0], 120)).toEqual([-1, 0]);
    expect(meanAxisDomain([], 120)).toEqual([-1, 0]);
  });
});

describe("meanLabelDigits", () => {
  test("narrow bars get two decimals, wide ones three", () => {
    expect(meanLabelDigits(30)).toBe(2);
    expect(meanLabelDigits(60)).toBe(3);
  });
});
