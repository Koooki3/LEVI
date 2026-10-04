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

describe("timeline clock", () => {
  test("shows hundredths as m:ss.cc", async () => {
    const { formatClockPrecise } =
      await import("@/components/viewer/time-format");
    expect(formatClockPrecise(0)).toBe("0:00.00");
    expect(formatClockPrecise(7.7)).toBe("0:07.70");
    expect(formatClockPrecise(72.345)).toBe("1:12.35");
    expect(formatClockPrecise(59.999)).toBe("1:00.00");
  });
});
