import { click, press, render, setupDom } from "@/components/ds/__tests__/dom";
import { act, useEffect, type ComponentProps } from "react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { FR3_JOINT_LIMITS } from "@/lib/robot-studio/fr3-kinematics";
import { ROBOTIQ_STROKE_M } from "@/lib/robot-studio/robotiq-2f85";
import { detectFr3Replay } from "@/lib/replay/fr3-columns";
import { TimeProvider, useTime } from "@/context/time-context";
import { LocaleProvider } from "@/components/levi-locale";
import { seriesColor } from "@/components/viewer/data-palette";

// The scene is three.js: a recorder takes its place (the same trick as the
// robot studio's test).
type SceneProps = ComponentProps<typeof import("../replay-scene").default>;
let scene: SceneProps | null = null;
// "fail": the model does not load; "throw": WebGL cannot start (a render error).
let behavior: "ok" | "fail" | "throw" = "ok";
let mounts = 0;
function FakeScene(props: SceneProps) {
  if (behavior === "throw") throw new Error("WebGL is not available");
  scene = props;
  const { onLoad } = props;
  useEffect(() => {
    mounts++;
  }, []);
  useEffect(() => onLoad(behavior !== "fail"), [onLoad]);
  return <div data-testid="fake-scene" />;
}
mock.module("next/dynamic", () => ({ default: () => FakeScene }));
const { default: ReplayPanel } = await import("../replay-panel");
setupDom();

type Time = ReturnType<typeof useTime>;
let time: Time;
function Grab() {
  time = useTime();
  return null;
}
const consoleError = console.error;
beforeEach(() => {
  scene = null;
  behavior = "ok";
  mounts = 0;
  window.sessionStorage.clear();
  window.localStorage.clear();
});
afterEach(() => {
  console.error = consoleError;
});

const keys = (feature: string, names: readonly string[]) =>
  names.map((name) => `${feature} | ${name}`);
const jointNames = Array.from({ length: 7 }, (_, i) => `joint_${i + 1}`);
const mid = (c: number) =>
  (FR3_JOINT_LIMITS[c].lower + FR3_JOINT_LIMITS[c].upper) / 2;

function jointRows(n: number, step = 0.01) {
  return Array.from({ length: n }, (_, i) => {
    const row: Record<string, number> = { timestamp: i * 0.5 };
    jointNames.forEach((name, c) => {
      row[`observation.state | ${name}`] = mid(c) + i * step;
      row[`action | ${name}`] = mid(c) - i * step;
    });
    row["observation.state | gripper"] = i < 5 ? 1 : 0;
    return row;
  });
}
const jointDetection = detectFr3Replay({ robot_type: "franka_panda" }, [
  "timestamp",
  ...keys("observation.state", [...jointNames, "gripper"]),
  ...keys("action", jointNames),
]);
const noActionDetection = detectFr3Replay({ robot_type: "franka_panda" }, [
  "timestamp",
  ...keys("observation.state", [...jointNames, "gripper"]),
]);
const poseNames = ["x", "y", "z", "rx", "ry", "rz", "gripper"];
const poseRows = Array.from({ length: 10 }, (_, i) => ({
  timestamp: i * 0.5,
  "observation.state | x": 0.3 + i * 0.01,
  "observation.state | y": 0,
  "observation.state | z": 0.4,
  "observation.state | rx": 3,
  "observation.state | ry": 0.1,
  "observation.state | rz": -0.2,
  "observation.state | gripper": 1,
}));
const poseDetection = detectFr3Replay({ robot_type: "generic_arm" }, [
  "timestamp",
  ...keys("observation.state", poseNames),
]);

const el = {} as never;
const video = { el, segmentStart: 0 };
function panel(props: Partial<ComponentProps<typeof ReplayPanel>> = {}) {
  return (
    <TimeProvider duration={10}>
      <Grab />
      <ReplayPanel
        detection={jointDetection}
        rows={jointRows(10)}
        video={video}
        {...props}
      />
    </TimeProvider>
  );
}
const button = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find((b) =>
    b.textContent?.includes(text),
  ) ?? null;

describe("ReplayPanel: opening and closing", () => {
  test("an FR3-family dataset opens with the scene, fed the packed rows and the page clock", async () => {
    const { host } = await render(panel());
    expect(host.querySelector('[data-testid="fake-scene"]')).not.toBeNull();
    expect(scene!.data.mode).toBe("joints");
    expect(scene!.data.rows).toBe(10);
    expect(scene!.video).toBe(video);
    expect(scene!.subscribe).toBe(time.subscribe);
    expect(host.textContent).toContain("Joint angles");
  });

  test("a generic arm (pose only) opens by default too, with the pose scene", async () => {
    const { host } = await render(
      panel({ detection: poseDetection, rows: poseRows }),
    );
    expect(host.querySelector('[data-testid="fake-scene"]')).not.toBeNull();
    expect(scene!.data.mode).toBe("pose");
    expect(button(host, "Hide 3D replay")!.getAttribute("aria-expanded")).toBe(
      "true",
    );
    await click(button(host, "Hide 3D replay"));
    expect(host.querySelector('[data-testid="fake-scene"]')).toBeNull();
    expect(button(host, "Show 3D replay")!.getAttribute("aria-expanded")).toBe(
      "false",
    );
  });

  test("a stored choice wins in either mode: closed stays closed, and Show opens it", async () => {
    window.sessionStorage.setItem("replay3dOpen", "0");
    const pose = await render(
      panel({ detection: poseDetection, rows: poseRows }),
    );
    expect(pose.host.querySelector('[data-testid="fake-scene"]')).toBeNull();
    await click(button(pose.host, "Show 3D replay"));
    expect(
      pose.host.querySelector('[data-testid="fake-scene"]'),
    ).not.toBeNull();
    expect(window.sessionStorage.getItem("replay3dOpen")).toBe("1");
    const joints = await render(panel());
    expect(
      joints.host.querySelector('[data-testid="fake-scene"]'),
    ).not.toBeNull();
  });

  test("closing unloads the scene, and the choice is kept for the next episode", async () => {
    const first = await render(panel());
    await click(button(first.host, "Hide 3D replay"));
    expect(first.host.querySelector('[data-testid="fake-scene"]')).toBeNull();
    // the next episode mounts the panel again
    const second = await render(panel());
    expect(second.host.querySelector('[data-testid="fake-scene"]')).toBeNull();
    expect(button(second.host, "Show 3D replay")).not.toBeNull();
  });

  test("opened later, the scene starts at the time the page is at", async () => {
    const { host } = await render(
      panel({ detection: poseDetection, rows: poseRows }),
    );
    await click(button(host, "Hide 3D replay"));
    await act(async () => time.seek(2.5));
    await click(button(host, "Show 3D replay"));
    expect(scene!.initialTime).toBe(2.5);
  });
});

describe("ReplayPanel: what it says", () => {
  test("joints: the row, the time and the seven angles in degrees at the page time", async () => {
    const { host } = await render(panel());
    await act(async () => time.seek(1.5)); // row index 3
    const text = host.textContent ?? "";
    expect(text).toContain("Row 4 of 10 · t = 1.50 s");
    const deg = (c: number) =>
      (((mid(c) + 3 * 0.01) * 180) / Math.PI).toFixed(1);
    expect(text).toContain(
      `Joints (deg): ${[0, 1, 2, 3, 4, 5, 6].map(deg).join(", ")}`,
    );
  });

  test("the viewport is a named group described by the readout", async () => {
    const { host } = await render(panel());
    const group = host.querySelector('.vw-r3d-viewport[role="group"]')!;
    expect(group.getAttribute("aria-label")).toBe(
      "FR3 arm replaying the episode",
    );
    const describedBy = group.getAttribute("aria-describedby")!;
    expect(host.querySelector(`[id="${describedBy}"]`)!.textContent).toContain(
      "Row 1 of 10",
    );
  });

  test("pose mode: says the frame is not verified and draws no joints", async () => {
    const { host } = await render(
      panel({ detection: poseDetection, rows: poseRows }),
    );
    const text = host.textContent ?? "";
    expect(text).toContain("End-effector pose only");
    expect(text).toContain("Pose frame not verified");
    expect(text).toContain("Position (m): 0.300, 0.000, 0.400");
    expect(text).toContain(
      "Rotation (rad, roll, pitch, yaw): 3.00, 0.10, -0.20",
    );
    expect(text).not.toContain("Joints (deg)");
  });

  test("a gripper column of unknown meaning (the DROID view's) is a bare value: no command, no polarity", async () => {
    // jointDetection is franka_panda with `observation.state | gripper`: the
    // DROID view's measured position, 0 = open and 1 = closed (inferred), the
    // opposite of LEVI's command. The panel must not say which way is open.
    const { host } = await render(panel());
    expect(jointDetection.gripperKind).toBe("unknown");
    const meter = host.querySelector('[role="meter"]')!;
    expect(meter.getAttribute("aria-label")).toBe("Gripper value");
    expect(meter.getAttribute("aria-valuenow")).toBe("1");
    const text = host.textContent ?? "";
    expect(text).toContain("Gripper: 1.00");
    expect(text).not.toContain("1 = open");
    expect(text).not.toContain("Gripper command");
    expect(text).not.toContain("a command, not a measured width");
    expect(text).toContain("does not say what its gripper column is");
    expect(text).toContain("There is no gripper model yet");
    await act(async () => time.seek(3)); // row 6
    expect(
      host.querySelector('[role="meter"]')!.getAttribute("aria-valuenow"),
    ).toBe("0");
    expect(host.textContent).toContain("Gripper: 0.00");
  });

  test("LEVI's own conversion (robot_type generic_arm): the gripper is the open = 1 command", async () => {
    const rows = poseRows.map((row, i) => ({
      ...row,
      "observation.state | gripper": i < 5 ? 1 : 0,
    }));
    expect(poseDetection.gripperKind).toBe("command");
    const { host } = await render(panel({ detection: poseDetection, rows }));
    const meter = host.querySelector('[role="meter"]')!;
    expect(meter.getAttribute("aria-label")).toBe("Gripper command");
    expect(meter.getAttribute("aria-valuenow")).toBe("1");
    const text = host.textContent ?? "";
    expect(text).toContain(
      "Gripper command: 1.00 (1 = open, 0 = closed; a command, not a measured width)",
    );
    expect(text).not.toContain("does not say what its gripper column is");
    expect(text).toContain("There is no gripper model yet");
    await act(async () => time.seek(3)); // row 6: closed
    expect(
      host.querySelector('[role="meter"]')!.getAttribute("aria-valuenow"),
    ).toBe("0");
  });

  test("a row with no gripper value: the bar is hidden from assistive technology, not a meter without a value", async () => {
    // role="meter" requires aria-valuenow, so a meter is only there with a value.
    const rows = jointRows(10).map((row, i) => {
      if (i >= 5) delete row["observation.state | gripper"];
      return row;
    });
    const { host } = await render(panel({ rows }));
    // (Counted, not compared: a failed toEqual on DOM nodes prints the whole tree.)
    const meters = () => Array.from(host.querySelectorAll('[role="meter"]'));
    expect(meters().length).toBe(1);
    expect(meters()[0].getAttribute("aria-valuenow")).toBe("1");
    await act(async () => time.seek(3)); // row 6: no value
    expect(meters().length).toBe(0);
    const bar = host.querySelector(".vw-r3d-gripper-bar")!;
    expect(bar.getAttribute("aria-hidden")).toBe("true");
    expect(bar.hasAttribute("aria-label")).toBe(false);
    expect(host.querySelector(".vw-r3d-gripper")!.textContent).toContain(
      "Gripper: –",
    );
    await act(async () => time.seek(1)); // back to a row with a value
    expect(meters().length).toBe(1);
    expect(meters()[0].getAttribute("aria-valuenow")).toBe("1");
  });

  test("a measured gripper width is shown in metres, against the stroke, and not as a command", async () => {
    const rows = jointRows(10).map((row, i) => ({
      ...row,
      "observation.gripper_width | gripper_width": i < 5 ? 0.042 : 0,
    }));
    const detection = detectFr3Replay({ robot_type: "franka_fr3" }, [
      "timestamp",
      ...keys("observation.state", [...jointNames, "gripper"]),
      "observation.gripper_width | gripper_width",
    ]);
    expect(detection.gripperKind).toBe("width");
    const { host } = await render(panel({ detection, rows }));
    const text = host.textContent ?? "";
    expect(text).toContain("Gripper opening, measured: 0.042 m (0 = closed)");
    expect(text).not.toContain("a command, not a measured width");
    const meter = host.querySelector('[role="meter"]')!;
    expect(meter.getAttribute("aria-label")).toBe("Measured gripper opening");
    expect(Number(meter.getAttribute("aria-valuenow"))).toBeCloseTo(
      0.042 / ROBOTIQ_STROKE_M,
      6,
    );
    await act(async () => time.seek(3));
    expect(host.textContent).toContain("Gripper opening, measured: 0.000 m");
  });

  test("a long episode is flagged as possibly thinned; joints beyond the limits are reported", async () => {
    const rows = jointRows(4000, 0);
    rows[7]["observation.state | joint_1"] = 50;
    const { host } = await render(panel({ rows }));
    const text = host.textContent ?? "";
    expect(text).toContain(
      "This episode has 4000 rows, the most the viewer keeps",
    );
    expect(text).toContain(
      "State rows with a joint beyond the FR3 limits (clamped to them): 1, 1 of them by more than 0.1 rad",
    );
  });

  test("fewer than two rows: a sentence instead of a viewport", async () => {
    const { host } = await render(panel({ rows: jointRows(1) }));
    expect(host.textContent).toContain("nothing to replay");
    expect(host.querySelector('[data-testid="fake-scene"]')).toBeNull();
  });

  test("in Chinese the panel speaks Chinese", async () => {
    window.localStorage.setItem("levi-language", "zh");
    const { host } = await render(<LocaleProvider>{panel()}</LocaleProvider>);
    const text = host.textContent ?? "";
    expect(text).toContain("三维回放");
    expect(text).toContain("关节角");
    expect(text).toContain("收起三维回放");
    // franka_panda: a gripper of unknown meaning, said neutrally.
    expect(text).toContain("夹爪：1.00");
    expect(text).not.toContain("夹爪指令");
    expect(text).not.toContain("viewer.replay3d");
  });

  test("in Chinese a LEVI command gripper says 1 is open, and a gripper of unknown meaning says nothing about it", async () => {
    window.localStorage.setItem("levi-language", "zh");
    const { host } = await render(
      <LocaleProvider>
        {panel({ detection: poseDetection, rows: poseRows })}
      </LocaleProvider>,
    );
    const text = host.textContent ?? "";
    expect(text).toContain(
      "夹爪指令：1.00（1 为张开，0 为闭合；是指令值，不是实测开度）",
    );
    expect(text).not.toContain("数据集没有说明夹爪列");
    const unknown = await render(<LocaleProvider>{panel()}</LocaleProvider>);
    expect(unknown.host.textContent).toContain("夹爪：1.00");
    expect(unknown.host.textContent).toContain("数据集没有说明夹爪列");
    expect(unknown.host.textContent).not.toContain("1 为张开");
  });
});

const radio = (host: HTMLElement, name: string) =>
  Array.from(host.querySelectorAll<HTMLElement>('[role="radio"]')).find((r) =>
    r.textContent?.includes(name),
  ) ?? null;
const switchNamed = (host: HTMLElement, name: string) =>
  Array.from(host.querySelectorAll<HTMLInputElement>('[role="switch"]')).find(
    (input) => input.closest("label")?.textContent?.includes(name),
  ) ?? null;

describe("ReplayPanel: switches and views", () => {
  test("show action is off at first and turns the scene's ghost on, with its legend", async () => {
    const { host } = await render(panel());
    expect(scene!.showAction).toBe(false);
    expect(host.textContent).toContain("Solid arm = observation.state");
    expect(host.textContent).not.toContain("Translucent arm = action");
    await click(switchNamed(host, "Show action"));
    expect(scene!.showAction).toBe(true);
    const text = host.textContent ?? "";
    expect(text).toContain(
      "Translucent arm = action, a commanded target, not a measurement",
    );
    expect(text).toContain("it leads the state by one frame only");
    await click(switchNamed(host, "Show action"));
    expect(scene!.showAction).toBe(false);
    expect(host.textContent).not.toContain("Translucent arm = action");
  });

  test("a dataset without action columns has no action switch", async () => {
    const { host } = await render(panel({ detection: noActionDetection }));
    expect(switchNamed(host, "Show action")).toBeNull();
    expect(switchNamed(host, "Trail")).not.toBeNull();
    expect(scene!.showAction).toBe(false);
  });

  test("an action given as a pose is described as axes", async () => {
    const rows = poseRows.map((row) => ({
      ...row,
      "action | x": 1,
      "action | y": 0,
      "action | z": 0.5,
      "action | rx": 0,
      "action | ry": 0,
      "action | rz": 0,
    }));
    const detection = detectFr3Replay({ robot_type: "generic_arm" }, [
      "timestamp",
      ...keys("observation.state", poseNames),
      ...keys("action", ["x", "y", "z", "rx", "ry", "rz"]),
    ]);
    const { host } = await render(panel({ detection, rows }));
    await click(switchNamed(host, "Show action"));
    expect(host.textContent).toContain(
      "Faint axes = action, a commanded target, not a measurement",
    );
    expect(host.textContent).toContain("Axes and path = observation.state");
  });

  test("the trail switch reaches the scene and adds its legend line", async () => {
    const { host } = await render(panel());
    expect(scene!.trail).toBe(false);
    await click(switchNamed(host, "Trail"));
    expect(scene!.trail).toBe(true);
    expect(host.textContent).toContain(
      "at most the last 300 end-effector positions",
    );
  });

  test("the view buttons set the camera view; choosing again re-applies it", async () => {
    const { host } = await render(panel());
    expect(scene!.view).toBe("perspective");
    expect(
      host.querySelector('[role="radiogroup"]')!.getAttribute("aria-label"),
    ).toBe("View");
    const revision = scene!.viewRevision;
    await click(radio(host, "Front"));
    expect(scene!.view).toBe("front");
    expect(scene!.viewRevision).toBeGreaterThan(revision);
    await click(radio(host, "Top"));
    expect(scene!.view).toBe("top");
    expect(radio(host, "Top")!.getAttribute("aria-checked")).toBe("true");
  });

  test("the choices are kept for the next episode", async () => {
    const first = await render(panel());
    await click(switchNamed(first.host, "Trail"));
    await click(switchNamed(first.host, "Show action"));
    await click(radio(first.host, "Top"));
    const second = await render(panel());
    expect(scene!.trail).toBe(true);
    expect(scene!.showAction).toBe(true);
    expect(scene!.view).toBe("top");
    expect(switchNamed(second.host, "Trail")!.checked).toBe(true);
  });

  test("a stored choice that makes no sense is ignored", async () => {
    window.sessionStorage.setItem(
      "replay3dPrefs",
      '{"view":"sideways","trail":"yes"}',
    );
    await render(panel());
    expect(scene!.view).toBe("perspective");
    expect(scene!.trail).toBe(false);
    window.sessionStorage.setItem("replay3dPrefs", "{not json");
    await render(panel());
    expect(scene!.view).toBe("perspective");
  });
});

describe("ReplayPanel: when the 3D view cannot start", () => {
  test("a model that does not load shows what happened and a Retry button that tries again", async () => {
    behavior = "fail";
    const { host } = await render(panel());
    const alert = host.querySelector('[role="alert"]')!;
    expect(alert.textContent).toContain("The 3D view could not start");
    expect(alert.textContent).toContain(
      "The video and the charts are not affected",
    );
    expect(host.querySelector('[data-testid="fake-scene"]')).toBeNull();
    behavior = "ok";
    const before = mounts;
    await click(button(host, "Retry"));
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(host.querySelector('[data-testid="fake-scene"]')).not.toBeNull();
    expect(mounts).toBe(before + 1);
  });

  test("a WebGL error while rendering the scene shows the same state; Retry mounts it again", async () => {
    console.error = () => {};
    behavior = "throw";
    const { host } = await render(panel());
    expect(host.querySelector('[role="alert"]')!.textContent).toContain(
      "could not start",
    );
    behavior = "ok";
    await click(button(host, "Retry"));
    expect(host.querySelector('[data-testid="fake-scene"]')).not.toBeNull();
  });

  test("the rest of the panel stays: readout and notes are still there", async () => {
    behavior = "fail";
    const { host } = await render(panel());
    expect(host.textContent).toContain("Row 1 of 10");
  });
});

describe("ReplayPanel: keyboard", () => {
  test("Space and the arrow keys on a panel control stay there; elsewhere they reach the page", async () => {
    const seen: string[] = [];
    const onKey = (event: KeyboardEvent) => seen.push(event.key);
    window.addEventListener("keydown", onKey);
    try {
      const { host } = await render(panel());
      const trail = switchNamed(host, "Trail")!;
      await press(trail, " ");
      await press(trail, "ArrowDown");
      await press(radio(host, "Perspective"), "ArrowUp");
      expect(seen).toEqual([]);
      // Focus on the viewport or the heading: the page's play/pause and episode keys work.
      await press(host.querySelector(".vw-r3d-viewport"), " ");
      await press(host.querySelector(".vw-r3d-readout"), "ArrowDown");
      expect(seen).toEqual([" ", "ArrowDown"]);
      // Other keys on a control are not swallowed either.
      await press(trail, "Escape");
      expect(seen.at(-1)).toBe("Escape");
    } finally {
      window.removeEventListener("keydown", onKey);
    }
  });
});

describe("ReplayPanel: text and motion", () => {
  const keys3d = (catalog: Record<string, string>) =>
    Object.keys(catalog).filter((key) => key.startsWith("viewer.replay3d."));
  const holes = (text: string) => (text.match(/\{\w+\}/g) ?? []).sort();

  test("every viewer.replay3d.* text exists in English and Chinese with the same holes", () => {
    const english = keys3d(en as Record<string, string>);
    expect(english.length).toBeGreaterThan(30);
    expect(keys3d(zh as Record<string, string>).sort()).toEqual(
      [...english].sort(),
    );
    for (const key of english) {
      const a = (en as Record<string, string>)[key];
      const b = (zh as Record<string, string>)[key];
      expect(a.trim(), key).not.toBe("");
      expect(b.trim(), key).not.toBe("");
      expect(holes(b), key).toEqual(holes(a));
      expect(/[\u4e00-\u9fff]/.test(b), `${key} has no Chinese`).toBe(true);
    }
  });

  test("every viewer.replay3d.* key the panel can show is in the block (none shows as a raw key)", async () => {
    // Joints with action, switches on, then a failure: the visible text never contains a key.
    const { host } = await render(panel());
    await click(switchNamed(host, "Show action"));
    await click(switchNamed(host, "Trail"));
    expect(host.textContent).not.toContain("viewer.replay3d");
    behavior = "fail";
    const failed = await render(panel());
    expect(failed.host.textContent).not.toContain("viewer.replay3d");
  });

  test("the panel's styles animate nothing, so prefers-reduced-motion has nothing to stop", () => {
    const css = readFileSync(
      join(import.meta.dir, "../replay-panel.css"),
      "utf8",
    );
    expect(css).not.toMatch(/animation|transition|@keyframes/);
  });

  test("the gripper bar is drawn in the colour of observation.state (series 0), not the action's", () => {
    const css = readFileSync(
      join(import.meta.dir, "../replay-panel.css"),
      "utf8",
    );
    const fillRule = /\.vw-r3d-gripper-bar::after\s*\{([^}]*)\}/.exec(css)![1];
    const fill = /background:\s*([^;]+);/.exec(fillRule)![1].trim();
    expect(fill).toBe(seriesColor(0));
    expect(fill).not.toBe(seriesColor(1));
  });

  test("the camera views jump; there is no camera animation to reduce", () => {
    const source = readFileSync(
      join(import.meta.dir, "../fr3-model.tsx"),
      "utf8",
    );
    expect(source).not.toMatch(/requestAnimationFrame|lerp|tween|gsap/);
  });
});
