import { describe, expect, test } from "bun:test";
import {
  entryFromSuggest,
  estimateSplit,
  roundHalfEven,
  shareOf,
} from "../selection-fields";

describe("estimateSplit (the server's quota rule)", () => {
  test("keeps the natural share", () => {
    const s = estimateSplit(20, null, 60, 40);
    expect([s.successes, s.failures]).toEqual([12, 8]);
  });
  test("meets an explicit share", () => {
    const s = estimateSplit(20, 0.5, 60, 40);
    expect([s.successes, s.failures]).toEqual([10, 10]);
    expect([s.shortSuccesses, s.shortFailures]).toEqual([0, 0]);
  });
  test("fills a short side from the other and reports it", () => {
    const s = estimateSplit(20, 0.5, 50, 6);
    expect([s.successes, s.failures]).toEqual([14, 6]);
    expect(s.shortFailures).toBe(4);
    const t = estimateSplit(10, 0.8, 2, 30);
    expect([t.successes, t.failures, t.shortSuccesses]).toEqual([2, 8, 6]);
  });
  test("takes what exists and uses unknown outcomes last", () => {
    expect(estimateSplit(null, null, 3, 2).successes).toBe(3);
    const s = estimateSplit(14, 0.5, 5, 5, 6);
    expect([s.successes + s.failures, s.unknown]).toEqual([10, 4]);
    expect(estimateSplit(4, 0.5, 0, 0, 10).unknown).toBe(4);
  });
  test("rounds half to even like Python", () => {
    expect(roundHalfEven(2.5)).toBe(2);
    expect(roundHalfEven(3.5)).toBe(4);
    expect(roundHalfEven(2.4)).toBe(2);
  });
});

describe("chooser defaults", () => {
  test("share names", () => {
    expect(shareOf(null)).toBe("natural");
    expect(shareOf(1)).toBe("success");
    expect(shareOf(0)).toBe("failure");
    expect(shareOf(0.6)).toBe("custom");
  });
  test("a suggestion that covers everything stores no count", () => {
    const info = {
      task: "a",
      available: 30,
      successes: 10,
      failures: 20,
      unknown: 0,
      already_used: 0,
      suggested_count: 30,
      earlier_counts: [],
    };
    expect(entryFromSuggest("a", info).count).toBeNull();
    expect(
      entryFromSuggest("a", { ...info, available: 500, suggested_count: 100 })
        .count,
    ).toBe(100);
  });
});
