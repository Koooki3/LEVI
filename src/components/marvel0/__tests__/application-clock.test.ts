import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { expect, test } from "bun:test";
import {
  advanceInSteps,
  installApplicationClock,
  installDomApplicationClock,
} from "./application-clock";

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

test("advanceInSteps settles Promise work between steps of at most 30 s", async () => {
  const clock = installApplicationClock();
  try {
    const start = Date.now();
    const seen: string[] = [];
    // A periodic read whose answer lands one Promise turn after it is asked.
    const read = setInterval(() => {
      const asked = Date.now() - start;
      seen.push(`ask@${asked}`);
      void Promise.resolve().then(() => seen.push(`answer@${asked}`));
    }, 30_000);
    await advanceInSteps(clock, 120_000, async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    clearInterval(read);
    expect(Date.now() - start).toBe(120_000);
    expect(seen).toEqual([
      "ask@30000",
      "answer@30000",
      "ask@60000",
      "answer@60000",
      "ask@90000",
      "answer@90000",
      "ask@120000",
      "answer@120000",
    ]);
    // A short jump is one plain step, and zero still settles once.
    let settled = 0;
    await advanceInSteps(clock, 799, async () => void (settled += 1));
    await advanceInSteps(clock, 0, async () => void (settled += 1));
    expect(settled).toBe(2);
    expect(Date.now() - start).toBe(120_799);
    await expect(
      advanceInSteps(clock, Infinity, async () => undefined),
    ).rejects.toThrow();
    await expect(
      advanceInSteps(clock, -1, async () => undefined),
    ).rejects.toThrow();
    await expect(
      advanceInSteps(clock, 5, async () => undefined, 0),
    ).rejects.toThrow();
    expect(Date.now() - start).toBe(120_799);
  } finally {
    clock.restore();
  }
});

test("a DOM file's clock leaves the native timers behind after an earlier DOM file in the same process", async () => {
  const native = globalThis.setTimeout;
  // an earlier DOM file: setupDom registered happy-dom and unregistered it at its end
  if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
  await GlobalRegistrator.unregister();
  expect(globalThis.setTimeout).toBe(native);
  // this file: the clock first (beforeAll), then setupDom's beforeAll finds happy-dom registered
  const clock = installDomApplicationClock();
  expect(GlobalRegistrator.isRegistered).toBe(true);
  expect(globalThis.setTimeout).not.toBe(native);
  // its end: afterAll(clock.restore) before setupDom's unregister
  clock.restore();
  await GlobalRegistrator.unregister();
  expect(globalThis.setTimeout).toBe(native);
  // the next file still has working timers
  await new Promise((resolve) => setTimeout(resolve, 0));
});
