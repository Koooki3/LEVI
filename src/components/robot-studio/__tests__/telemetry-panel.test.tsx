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
      expect(calls).toHaveLength(0);
      await click(button(host, "Read snapshots"));
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
      await click(button(host, "Read snapshots"));
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
    await click(button(host, "Read snapshots"));
    await flush();
    expect(optIns).toEqual([true]);
    expect(host.textContent).toContain("Gripper model unavailable");
    await click(button(host, "Follow robot"));
    expect(follows.at(-1)?.joints_rad).toEqual([...FR3_INITIAL_JOINTS]);
    await rerender(<TelemetryPanel {...props} following />);
    expect(button(host, "Follow robot").disabled).toBe(true);
    await click(button(host, "Edit target from measured pose"));
    expect(edits).toHaveLength(1);
    clock = 1000;
    await click(button(host, "Edit target from measured pose"));
    expect(edits).toHaveLength(1);
    await rerender(<TelemetryPanel {...props} />);
    await click(button(host, "Follow robot"));
    expect(follows.at(-1)).toBeNull();
    await click(button(host, "Stop reading"));
    expect(optIns).toEqual([true, false]);
  } finally {
    globalThis.fetch = original;
    time.mockRestore();
  }
});
