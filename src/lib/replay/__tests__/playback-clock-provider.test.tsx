import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act, useEffect } from "react";
import { describe, expect, test } from "bun:test";
import { TimeProvider, useTime } from "@/context/time-context";
import { startReplayClock } from "../playback-clock";

setupDom();

// The real TimeProvider: its `updateTime` runs every listener in a
// `forEach` and only then sets the page's own time, so a listener that throws
// would stop the seek that called it.
type Time = ReturnType<typeof useTime>;
let time: Time;
function Clock({ onTime }: { onTime: (t: number) => void }) {
  time = useTime();
  const { subscribe } = time;
  useEffect(
    () =>
      startReplayClock({
        subscribe,
        video: null,
        onTime,
        onError: () => {},
      }),
    [subscribe, onTime],
  );
  return <output>{time.currentTime.toFixed(2)}</output>;
}

describe("the replay clock inside the page's TimeProvider", () => {
  test("an onTime that throws does not stop a seek: the page's time still moves and later seeks still reach onTime", async () => {
    const seen: number[] = [];
    const onTime = (t: number) => {
      seen.push(t);
      throw new RangeError("Expected seven finite FR3 joints");
    };
    const { host } = await render(
      <TimeProvider duration={10}>
        <Clock onTime={onTime} />
      </TimeProvider>,
    );
    await act(async () => time.seek(2));
    expect(host.querySelector("output")!.textContent).toBe("2.00");
    await act(async () => time.seek(7.5));
    expect(host.querySelector("output")!.textContent).toBe("7.50");
    expect(seen).toEqual([2, 7.5]);
  });

  test("a playing video's seek also reaches the other listeners when onTime throws", async () => {
    const others: number[] = [];
    const onTime = () => {
      throw new Error("boom");
    };
    await render(
      <TimeProvider duration={10}>
        <Clock onTime={onTime} />
      </TimeProvider>,
    );
    // Registered after the clock: before the fix it was never called.
    const off = time.subscribe((t) => others.push(t));
    await act(async () => time.seek(3, "video"));
    off();
    expect(others).toEqual([3]);
  });
});
