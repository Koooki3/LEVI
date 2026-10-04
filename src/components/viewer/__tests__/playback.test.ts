import { describe, expect, test } from "bun:test";
import { formatClock } from "@/components/playback-bar";

describe("playback clock", () => {
  test("shows whole seconds as m:ss", () => {
    expect(formatClock(0)).toBe("0:00");
    expect(formatClock(12.9)).toBe("0:12");
    expect(formatClock(75)).toBe("1:15");
    expect(formatClock(-3)).toBe("0:00");
  });
});
