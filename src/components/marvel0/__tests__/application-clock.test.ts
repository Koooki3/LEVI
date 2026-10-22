import { expect, test } from "bun:test";
import { installApplicationClock } from "./application-clock";

test("orders deadlines, cancels, and handles self-cancelling intervals and nested timers", () => {
  const clock = installApplicationClock();
  try {
    const start = Date.now();
    const seen: Array<[string, number]> = [];
    const record = (name: string) => seen.push([name, Date.now() - start]);
    const removed = setTimeout(() => record("removed"), 1);
    clearTimeout(removed);
    setTimeout(() => {
      record("a");
      setTimeout(() => record("nested"), 2);
    }, 5);
    setTimeout(() => record("b"), 5);
    const repeating = setInterval(() => {
      record("interval");
      clearInterval(repeating);
    }, 5);
    clock.advance(10);
    expect(seen).toEqual([
      ["a", 5],
      ["b", 5],
      ["interval", 5],
      ["nested", 7],
    ]);
    expect(Date.now() - start).toBe(10);
    expect(() => clock.advance(-1)).toThrow();
    expect(() => clock.advance(Infinity)).toThrow();
    expect(Date.now() - start).toBe(10);
  } finally {
    clock.restore();
  }
});

test("bounds zero-delay recursion and rejects reentrant advance", () => {
  const clock = installApplicationClock();
  try {
    setTimeout(() => {
      expect(() => clock.advance(0)).toThrow();
    }, 0);
    clock.advance(0);
    const again = () => {
      setTimeout(again, 0);
    };
    setTimeout(again, 0);
    expect(() => clock.advance(0)).toThrow("callback limit");
  } finally {
    clock.restore();
  }
});

test("restores descriptors while captured host clocks and native timers stay real", async () => {
  const keys = [
    "setTimeout",
    "clearTimeout",
    "setInterval",
    "clearInterval",
    "performance",
  ] as const;
  const descriptors = keys.map((key) =>
    Object.getOwnPropertyDescriptor(globalThis, key),
  );
  const dateDescriptor = Object.getOwnPropertyDescriptor(Date, "now");
  const host = performance;
  const hostNowDescriptor = Object.getOwnPropertyDescriptor(host, "now");
  const hostNow = host.now();
  const immediate = setImmediate;
  const nativeTimeout = setTimeout;
  const clock = installApplicationClock();
  try {
    const appNow = performance.now();
    clock.advance(30000);
    expect(performance.now() - appNow).toBe(30000);
    expect(performance).not.toBe(host);
    expect(Object.getOwnPropertyDescriptor(host, "now")).toEqual(
      hostNowDescriptor,
    );
    expect(host.now() - hostNow).toBeLessThan(30000);
    expect(setImmediate).toBe(immediate);
    await new Promise<void>((resolve) => nativeTimeout(resolve, 0));
    await new Promise<void>((resolve) => immediate(resolve));
  } finally {
    clock.restore();
  }
  expect(
    keys.map((key) => Object.getOwnPropertyDescriptor(globalThis, key)),
  ).toEqual(descriptors);
  expect(Object.getOwnPropertyDescriptor(Date, "now")).toEqual(dateDescriptor);
  expect(performance).toBe(host);
  clock.restore();
  expect(() => clock.advance(0)).toThrow();
});
