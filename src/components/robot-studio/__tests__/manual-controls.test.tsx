import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { act } from "react";
import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import {
  FR3_INITIAL_JOINTS,
  forwardPose,
} from "@/lib/robot-studio/fr3-kinematics";
import ManualControls, { MANUAL_EXTRA_LIMIT_KEYS } from "../manual-controls";
import type { ManualAnchor, ManualApi, ManualView } from "../manual-api";
import type { ManualDragController } from "../manual-session";
import { EMPTY_TELEOP_DRAFT } from "../tool-draft";

setupDom();
const text = (key: string) =>
  (en as Record<string, string>)["robotStudio.manual." + key] ??
  "robotStudio.manual." + key;
const button = (host: HTMLElement, key: string) =>
  Array.from(host.querySelectorAll("button")).find(
    (element) => element.textContent?.trim() === text(key),
  )!;
const doc = (active = false): ManualView => ({
  schema: "levi.marvel0.manual.v1",
  state: active ? "owned_idle" : "ended",
  environment: "synthetic",
  capabilities: {
    real_motion: false,
    synthetic_only: true,
    can_start: !active,
  },
  reason_code: "",
  admission: active ? "open" : "closed",
  session_id: active ? "synthetic-ui" : null,
  start_request_id: null,
  generation: active ? 1 : null,
  anchor_id: active ? "synthetic-anchor" : null,
  anchor: active
    ? {
        position: forwardPose(FR3_INITIAL_JOINTS).position,
        quaternion: forwardPose(FR3_INITIAL_JOINTS).quaternion,
        joints_rad: [...FR3_INITIAL_JOINTS],
        sampled_at_mono: 1,
      }
    : null,
  counters: {},
  stop_evidence: null,
});
function fakeApi(production = false) {
  const calls: string[] = [];
  const api: ManualApi = {
    status: async () => {
      calls.push("GET");
      return production
        ? {
            ...doc(),
            state: "unavailable",
            environment: "production",
            reason_code: "stop_unproven",
            capabilities: {
              can_start: false,
              real_motion: false,
              synthetic_only: true,
            },
          }
        : doc();
    },
    challenge: async () => {
      calls.push("challenge");
      return { challenge: "fixture", confirm: "SYNTHETIC" };
    },
    start: async (body) => {
      calls.push("start");
      return { ...doc(true), start_request_id: body.request_id };
    },
    deadman: async () => {
      calls.push("deadman");
      return doc(true);
    },
    target: async () => {
      calls.push("target");
      return doc(true);
    },
    stop: async () => {
      calls.push("stop");
      return { ...doc(true), state: "held", admission: "closed" };
    },
    end: async () => {
      calls.push("end");
      return { ...doc(true), state: "ended", admission: "closed" };
    },
  };
  return { api, calls };
}
async function type(input: HTMLInputElement, value: string) {
  await act(async () => {
    input.focus();
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(
      new KeyboardEvent("keyup", { key: "e", bubbles: true }),
    );
  });
}
const base = {
  workspaceBox: {
    min: [-2, -2, -2] as [number, number, number],
    max: [2, 2, 2] as [number, number, number],
  },
  teleopDraft: {
    ...EMPTY_TELEOP_DRAFT,
    linearSpeedMps: 0.03,
    positionStepMm: 1,
  },
  targetPosition: forwardPose(FR3_INITIAL_JOINTS).position,
  canDrag: true,
};

describe("manual controls require a person and preserve stop gates", () => {
  test("mount reads only GET and presents stop_unproven before missing configuration", async () => {
    const f = fakeApi(true);
    const { host } = await render(
      <ManualControls
        {...base}
        api={f.api}
        onSessionAnchor={() => {}}
        onController={() => {}}
      />,
    );
    await waitFor(() => host.textContent?.includes(text("stopUnproven")));
    expect(button(host, "review").disabled).toBe(true);
    expect(f.calls).toEqual(["GET"]);
    const fields = Array.from(
      host.querySelectorAll<HTMLInputElement>('input[type="number"]'),
    );
    expect(fields).toHaveLength(9);
    expect(fields.every((field) => field.value === "")).toBe(true);
  });

  test("a synthetic capability cannot invent the shared box, speed or step", async () => {
    const f = fakeApi();
    const { host } = await render(
      <ManualControls
        {...base}
        workspaceBox={null}
        teleopDraft={EMPTY_TELEOP_DRAFT}
        api={f.api}
        onSessionAnchor={() => {}}
        onController={() => {}}
      />,
    );
    await waitFor(() => host.querySelector('[data-phase="ready"]'));
    expect(button(host, "review").disabled).toBe(true);
    expect(f.calls).toEqual(["GET"]);
    expect(host.querySelectorAll(".rs-workspace-editor")).toHaveLength(0);
  });

  for (const event of [
    "pointerup",
    "pointercancel",
    "lostpointercapture",
    "blur",
    "hidden",
    "unmount",
  ]) {
    test(
      event + " closes the held 3D drag without a second mouse button",
      async () => {
        const f = fakeApi();
        const holder = { current: null as ManualDragController | null };
        const anchors: (ManualAnchor | null)[] = [];
        const { host, rerender } = await render(
          <ManualControls
            {...base}
            api={f.api}
            onSessionAnchor={(anchor) => anchors.push(anchor)}
            onController={(bridge) => {
              holder.current = bridge;
            }}
          />,
        );
        await waitFor(() => host.querySelector('[data-phase="ready"]'));
        const values = [0.2, 0.02, 0.3, 2, 0.2, 0.2, 0.02, 0.001, 0.3];
        for (let i = 0; i < MANUAL_EXTRA_LIMIT_KEYS.length; i++) {
          const input = Array.from(
            host.querySelectorAll<HTMLInputElement>('input[type="number"]'),
          )[i];
          await type(input, String(values[i]));
        }
        expect(button(host, "review").disabled).toBe(false);
        await click(button(host, "review"));
        await waitFor(() => host.querySelector('[data-phase="review"]'));
        await type(
          host.querySelector<HTMLInputElement>(
            'input[aria-label="' + text("confirmInput") + '"]',
          )!,
          "SYNTHETIC",
        );
        for (const checkbox of host.querySelectorAll('input[type="checkbox"]'))
          await click(checkbox);
        await click(button(host, "start"));
        await waitFor(() => host.querySelector('[data-phase="armed"]'));
        expect(f.calls.includes("deadman")).toBe(false);
        await act(async () => {
          expect(holder.current!.beginDrag()).toBe(true);
        });
        await waitFor(() => f.calls.includes("deadman"));
        // Anchor handoff is once per session, not again for holding/status renders.
        expect(anchors.filter((anchor) => anchor !== null)).toHaveLength(1);
        const before = f.calls.filter((call) => call === "target").length;
        const visibility = Object.getOwnPropertyDescriptor(
          document,
          "visibilityState",
        );
        try {
          if (event === "unmount") await rerender(<></>);
          else if (event === "hidden") {
            Object.defineProperty(document, "visibilityState", {
              configurable: true,
              value: "hidden",
            });
            await act(async () => {
              document.dispatchEvent(new Event("visibilitychange"));
            });
          } else {
            await act(async () => {
              window.dispatchEvent(new Event(event));
            });
          }
          await waitFor(() => f.calls.includes("end"));
          expect(f.calls.filter((call) => call === "stop")).toHaveLength(1);
          expect(f.calls.filter((call) => call === "target").length).toBe(
            before,
          );
          if (event === "unmount") expect(holder.current).toBeNull();
          else expect(holder.current!.beginDrag()).toBe(false);
        } finally {
          if (visibility)
            Object.defineProperty(document, "visibilityState", visibility);
          else Reflect.deleteProperty(document, "visibilityState");
          await rerender(<></>);
        }
      },
    );
  }
});

test("manual counters and stop evidence are explicit and never inferred from closed admission", async () => {
  const f = fakeApi(true);
  f.api.status = async () => ({
    ...doc(),
    state: "unavailable",
    environment: "production",
    reason_code: "stop_unproven",
    admission: "closed",
    capabilities: {
      can_start: false,
      real_motion: false,
      synthetic_only: true,
    },
    counters: { accepted: 3, sent: 2, acknowledged: 1, settled: 0 },
    stop_evidence: "stop_unproven",
  });
  const { host } = await render(
    <ManualControls
      {...base}
      api={f.api}
      onSessionAnchor={() => {}}
      onController={() => {}}
    />,
  );
  await waitFor(
    () =>
      host.querySelector('[data-counter="accepted"] dd')?.textContent === "3",
  );
  expect(host.querySelector('[data-counter="sent"] dd')?.textContent).toBe("2");
  expect(
    host.querySelector('[data-counter="acknowledged"] dd')?.textContent,
  ).toBe("1");
  expect(host.querySelector('[data-counter="settled"] dd')?.textContent).toBe(
    "0",
  );
  expect(host.querySelector(".rs-manual-stop-evidence code")?.textContent).toBe(
    "stop_unproven",
  );
  expect(button(host, "review").disabled).toBe(true);
});
