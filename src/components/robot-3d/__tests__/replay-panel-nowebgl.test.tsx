// The panel with the real scene and the real <Canvas> where WebGL cannot be
// created: the error state with Retry comes up by itself (no fake scene that
// throws), and Retry mounts a new scene that works once WebGL is back.
// Only the renderer is a stand-in (happy-dom has no WebGL): it is what the
// guard builds, so the guard's failure path is the real one.
import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  mock,
  test,
} from "bun:test";
import type * as THREE from "three";
import { detectFr3Replay } from "@/lib/replay/fr3-columns";
import { TimeProvider } from "@/context/time-context";
import { LocaleProvider } from "@/components/levi-locale";
import { createGuardedRenderer as realGuard } from "../webgl-guard";

// Kept before the mock below replaces the module (bun updates live bindings).
const guard = realGuard;

let webgl = false;
// A renderer that accepts anything R3F does to it.
const anything = (): unknown =>
  new Proxy(function () {}, {
    get: (_target, key) => (key === "then" ? undefined : anything()),
    set: () => true,
    apply: () => anything(),
  });
const stub = new Proxy(
  { render() {} },
  {
    get: (target, key) => (key in target ? (target as never)[key] : anything()),
    set: () => true,
  },
) as unknown as THREE.WebGLRenderer;
let attempts = 0;
mock.module("../webgl-guard", () => ({
  createGuardedRenderer: (
    extra: Parameters<typeof guard>[0],
    onFailure: Parameters<typeof guard>[1],
  ) =>
    guard(extra, onFailure, () => {
      attempts++;
      if (!webgl) throw new Error("Error creating WebGL context.");
      return stub;
    }),
}));
// next/dynamic would load the scene lazily; here it is the real scene at once.
const { default: RealScene } = await import("../replay-scene");
mock.module("next/dynamic", () => ({ default: () => RealScene }));
const { default: ReplayPanel } = await import("../replay-panel");
setupDom();

beforeAll(() => {
  HTMLElement.prototype.getBoundingClientRect = function () {
    return new DOMRect(0, 0, 400, 300);
  };
});
const consoleWarn = console.warn;
const consoleError = console.error;
beforeEach(() => {
  webgl = false;
  attempts = 0;
  window.sessionStorage.clear();
  console.warn = () => {};
  console.error = () => {};
});
afterEach(() => {
  console.warn = consoleWarn;
  console.error = consoleError;
});
const rejections: unknown[] = [];
const onRejection = (reason: unknown) => rejections.push(reason);
process.on("unhandledRejection", onRejection);
afterAll(() => process.off("unhandledRejection", onRejection));

const poseNames = ["x", "y", "z", "rx", "ry", "rz"];
const detection = detectFr3Replay({ robot_type: "generic_arm" }, [
  "timestamp",
  ...poseNames.map((n) => `observation.state | ${n}`),
]);
const rows = Array.from({ length: 6 }, (_, i) => ({
  timestamp: i * 0.5,
  "observation.state | x": 0.3 + i * 0.01,
  "observation.state | y": 0,
  "observation.state | z": 0.4,
  "observation.state | rx": 3,
  "observation.state | ry": 0.1,
  "observation.state | rz": 0,
}));

const settle = async () => {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
  }
};
const ui = (
  <LocaleProvider>
    <TimeProvider duration={3}>
      <ReplayPanel detection={detection} rows={rows} video={null} />
    </TimeProvider>
  </LocaleProvider>
);

describe("ReplayPanel where WebGL cannot be created (real scene, real Canvas)", () => {
  test("pose mode: the alert with Retry comes up by itself, nothing is thrown, one attempt", async () => {
    const { host } = await render(ui);
    await settle();
    const alert = host.querySelector('[role="alert"]');
    expect(alert).not.toBeNull();
    expect(alert!.textContent).toContain("The 3D view could not start");
    expect(
      Array.from(alert!.querySelectorAll("button")).some(
        (b) => b.textContent === "Retry",
      ),
    ).toBe(true);
    expect(attempts).toBe(1);
    expect(rejections).toEqual([]);
  });

  test("Retry with WebGL back mounts the scene and clears the alert", async () => {
    const { host } = await render(ui);
    await settle();
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
    webgl = true;
    const retry = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent === "Retry",
    );
    await click(retry!);
    await settle();
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(host.querySelector("canvas")).not.toBeNull();
    expect(attempts).toBe(2);
    expect(rejections).toEqual([]);
  });

  test("Retry with WebGL still missing shows the alert again, one new attempt", async () => {
    const { host } = await render(ui);
    await settle();
    const retry = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent === "Retry",
    );
    await click(retry!);
    await settle();
    expect(host.querySelector('[role="alert"]')).not.toBeNull();
    expect(attempts).toBe(2);
  });

  test("WebGL available from the start: no alert (the regression guard)", async () => {
    webgl = true;
    const { host } = await render(ui);
    await settle();
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(host.querySelector("canvas")).not.toBeNull();
    expect(attempts).toBe(1);
  });
});
