import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { act, useEffect, type ComponentProps } from "react";
import { beforeEach, describe, expect, mock, test } from "bun:test";
import { FR3_JOINT_LIMITS } from "@/lib/robot-studio/fr3-kinematics";
import { detectFr3Replay } from "@/lib/replay/fr3-columns";
import { TimeProvider, useTime } from "@/context/time-context";
import { LocaleProvider } from "@/components/levi-locale";

// The scene is three.js: a recorder takes its place (the same trick as the
// robot studio's test).
type SceneProps = ComponentProps<typeof import("../replay-scene").default>;
let scene: SceneProps | null = null;
function FakeScene(props: SceneProps) {
  scene = props;
  const { onLoad } = props;
  useEffect(() => onLoad(true), [onLoad]);
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
beforeEach(() => {
  scene = null;
  window.sessionStorage.clear();
  window.localStorage.clear();
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
    });
    row["observation.state | gripper"] = i < 5 ? 1 : 0;
    return row;
  });
}
const jointDetection = detectFr3Replay({ robot_type: "franka_panda" }, [
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
        family="fr3"
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

  test("a generic arm starts closed: no scene is loaded until it is opened", async () => {
    const { host } = await render(
      panel({ detection: poseDetection, rows: poseRows, family: "generic" }),
    );
    expect(host.querySelector('[data-testid="fake-scene"]')).toBeNull();
    expect(button(host, "Show 3D replay")!.getAttribute("aria-expanded")).toBe(
      "false",
    );
    await click(button(host, "Show 3D replay"));
    expect(host.querySelector('[data-testid="fake-scene"]')).not.toBeNull();
    expect(scene!.data.mode).toBe("pose");
    expect(button(host, "Hide 3D replay")!.getAttribute("aria-expanded")).toBe(
      "true",
    );
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
      panel({ detection: poseDetection, rows: poseRows, family: "generic" }),
    );
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
      panel({ detection: poseDetection, rows: poseRows, family: "generic" }),
    );
    await click(button(host, "Show 3D replay"));
    const text = host.textContent ?? "";
    expect(text).toContain("End-effector pose only");
    expect(text).toContain("Pose frame not verified");
    expect(text).toContain("Position (m): 0.300, 0.000, 0.400");
    expect(text).toContain(
      "Rotation (rad, roll, pitch, yaw): 3.00, 0.10, -0.20",
    );
    expect(text).not.toContain("Joints (deg)");
  });

  test("the gripper is a command value in a 0..1 meter, with the no-model note", async () => {
    const { host } = await render(panel());
    const meter = host.querySelector('[role="meter"]')!;
    expect(meter.getAttribute("aria-valuenow")).toBe("1");
    expect(host.textContent).toContain(
      "Gripper command: 1.00 (1 = open, 0 = closed; a command, not a measured width)",
    );
    expect(host.textContent).toContain("There is no gripper model yet");
    await act(async () => time.seek(3)); // row 6: closed
    expect(
      host.querySelector('[role="meter"]')!.getAttribute("aria-valuenow"),
    ).toBe("0");
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
    expect(text).toContain("夹爪指令");
    expect(text).not.toContain("viewer.replay3d");
  });
});
