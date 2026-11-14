import { click, flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { describe, expect, spyOn, test } from "bun:test";
import TelemetryPanel from "../telemetry-panel";
import { FR3_INITIAL_JOINTS } from "@/lib/robot-studio/fr3-kinematics";
import type {
  RobotStudioMeasurement,
  RobotStudioState,
} from "@/types/robotStudio";

setupDom();
const sample = (): RobotStudioState => ({
  schema: "levi.robot_studio.state.v1",
  state: "fresh",
  reason_code: "sample_fresh",
  sampled_at_epoch: 1800000000,
  age_s: 0.2,
  source_id: "fixture",
  sequence: 1,
  measurement: {
    joints_rad: [...FR3_INITIAL_JOINTS],
    gripper: null,
    reported_ee: null,
  },
  capabilities: { read_only: true, motion: false },
});
const button = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find((x) =>
    x.textContent?.includes(text),
  )!;

describe("read-only snapshot polling", () => {
  test("request latency is counted before measured data can be copied", async () => {
    const original = globalThis.fetch;
    let clock = 0;
    const time = spyOn(performance, "now").mockImplementation(() => clock);
    let complete: (response: Response) => void = () => {};
    const calls: RequestInit[] = [];
    globalThis.fetch = ((_url: string, init: RequestInit) => {
      calls.push(init);
      return new Promise<Response>((resolve) => {
        complete = resolve;
      });
    }) as unknown as typeof fetch;
    const received: (RobotStudioMeasurement | null)[] = [];
    try {
      const { host } = await render(
        <TelemetryPanel
          onMeasurement={(m) => received.push(m)}
          onUseMeasurement={() => {
            throw new Error("Expired state copied");
          }}
          onEnabledChange={() => {}}
          following={false}
          onFollowMeasurement={() => {}}
          canUsePreview
        />,
      );
      expect(calls).toHaveLength(1);
      expect(calls[0].method).toBe("GET");
      await act(async () => {
        clock = 900;
        complete(new Response(JSON.stringify(sample()), { status: 200 }));
        await Promise.resolve();
      });
      await flush();
      expect(host.textContent).toContain("Sample expired");
      expect(button(host, "Copy measured pose").disabled).toBe(true);
      expect(received.every((m) => m === null)).toBe(true);
    } finally {
      globalThis.fetch = original;
      time.mockRestore();
    }
  });
  test("copy checks freshness at click time as well as at the last render", async () => {
    const original = globalThis.fetch;
    let clock = 0;
    const time = spyOn(performance, "now").mockImplementation(() => clock);
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(sample()), { status: 200 })) as typeof fetch;
    let copies = 0;
    try {
      const { host } = await render(
        <TelemetryPanel
          onMeasurement={() => {}}
          onUseMeasurement={() => {
            copies++;
          }}
          onEnabledChange={() => {}}
          following={false}
          onFollowMeasurement={() => {}}
          canUsePreview
        />,
      );
      await flush();
      expect(button(host, "Copy measured pose").disabled).toBe(false);
      clock = 1000;
      await click(button(host, "Copy measured pose"));
      expect(copies).toBe(0);
    } finally {
      globalThis.fetch = original;
      time.mockRestore();
    }
  });
});

test("follow and edit actions recheck freshness and report opt-in changes", async () => {
  const original = globalThis.fetch;
  let clock = 0;
  const time = spyOn(performance, "now").mockImplementation(() => clock);
  globalThis.fetch = (async () =>
    new Response(JSON.stringify(sample()), { status: 200 })) as typeof fetch;
  const optIns: boolean[] = [];
  const follows: (RobotStudioMeasurement | null)[] = [];
  const edits: RobotStudioMeasurement[] = [];
  const onMeasurement = () => {};
  const props = {
    onMeasurement,
    onUseMeasurement: (m: RobotStudioMeasurement) => edits.push(m),
    onEnabledChange: (enabled: boolean) => optIns.push(enabled),
    following: false,
    onFollowMeasurement: (m: RobotStudioMeasurement | null) => follows.push(m),
    canUsePreview: true,
  };
  try {
    const { host, rerender } = await render(<TelemetryPanel {...props} />);
    await flush();
    expect(optIns).toEqual([]);
    expect(host.textContent).toContain("Gripper model unavailable");
    await click(button(host, "Follow actual robot"));
    expect(follows.at(-1)?.joints_rad).toEqual([...FR3_INITIAL_JOINTS]);
    await rerender(<TelemetryPanel {...props} following />);
    expect(button(host, "Follow actual robot").disabled).toBe(true);
    await click(button(host, "Edit target from actual pose"));
    expect(edits).toHaveLength(1);
    clock = 1000;
    await click(button(host, "Edit target from actual pose"));
    expect(edits).toHaveLength(1);
    await rerender(<TelemetryPanel {...props} />);
    await click(button(host, "Follow actual robot"));
    expect(follows.at(-1)).toBeNull();
    await click(button(host, "Stop reading"));
    expect(optIns).toEqual([false]);
  } finally {
    globalThis.fetch = original;
    time.mockRestore();
  }
});

function setHidden(value: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    get: () => value,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("a hidden tab and screen readers", () => {
  const props = {
    onMeasurement: () => {},
    onUseMeasurement: () => {},
    onEnabledChange: () => {},
    following: false,
    onFollowMeasurement: () => {},
    canUsePreview: true,
  };
  test("the changing age is not a live region; only the status word is", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(sample()), { status: 200 })) as typeof fetch;
    try {
      const { host } = await render(<TelemetryPanel {...props} />);
      await flush();
      for (const dl of host.querySelectorAll("dl"))
        expect(dl.closest("[aria-live]")).toBeNull();
      const status = host.querySelector("[role=status]")!;
      expect(status.className).toContain("rs-state-status");
      expect(status.textContent).not.toMatch(/\d+ ms/);
    } finally {
      globalThis.fetch = original;
    }
  });
  test("polling stops while the tab is hidden and resumes at once when shown", async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = (async () => {
      calls += 1;
      return new Response(JSON.stringify(sample()), { status: 200 });
    }) as typeof fetch;
    try {
      await render(<TelemetryPanel {...props} />);
      await flush();
      await act(async () => setHidden(true));
      await flush(50);
      const atHide = calls;
      await flush(1500);
      expect(calls).toBe(atHide);
      await act(async () => setHidden(false));
      await flush();
      expect(calls).toBeGreaterThan(atHide);
    } finally {
      globalThis.fetch = original;
      await act(async () => setHidden(false));
    }
  });
});
