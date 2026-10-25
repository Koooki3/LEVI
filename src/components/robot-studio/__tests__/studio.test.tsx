import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { act, useEffect, type ComponentProps } from "react";
import { describe, expect, mock, test } from "bun:test";
import { ROBOTIQ_STROKE_M } from "@/lib/robot-studio/robotiq-2f85";
import type { RobotStudioMeasurement } from "@/types/robotStudio";
import {
  forwardPose,
  FR3_INITIAL_JOINTS,
} from "@/lib/robot-studio/fr3-kinematics";

type SceneProps = ComponentProps<typeof import("../scene").default>;
let scene: SceneProps;
function FakeScene(props: SceneProps) {
  scene = props;
  const { onLoad } = props;
  useEffect(() => {
    onLoad(true);
  }, [onLoad]);
  return <div data-testid="fake-canvas" />;
}
mock.module("next/dynamic", () => ({ default: () => FakeScene }));
type WorkspaceProps = ComponentProps<
  typeof import("../workspace-panel").default
>;
type TelemetryProps = ComponentProps<
  typeof import("../telemetry-panel").default
>;
type ToolPanelProps = ComponentProps<typeof import("../tool-panel").default>;
let toolPanel: ToolPanelProps;
mock.module("../tool-panel", () => ({
  TeleopDraftEditor: () => null,
  default: (props: ToolPanelProps) => {
    toolPanel = props;
    return null;
  },
}));
mock.module("../gripper-controls", () => ({ default: () => null }));
let workspace: WorkspaceProps;
let telemetry: TelemetryProps;
type ManualProps = ComponentProps<typeof import("../manual-controls").default>;
let manualPanel: ManualProps;
const manualBridge = {
  beginDrag: mock(() => true),
  target: mock((position: [number, number, number]) => {
    void position;
  }),
  revoke: mock((reason: string) => {
    void reason;
  }),
};
function FakeManual(props: ManualProps) {
  manualPanel = props;
  const { onController } = props;
  useEffect(() => {
    onController(manualBridge);
    return () => onController(null);
  }, [onController]);
  return null;
}
mock.module("../manual-controls", () => ({ default: FakeManual }));
mock.module("../workspace-panel", () => ({
  default: (props: WorkspaceProps) => {
    workspace = props;
    return null;
  },
}));
mock.module("../telemetry-panel", () => ({
  default: (props: TelemetryProps) => {
    telemetry = props;
    return null;
  },
}));
const { default: RobotStudio } = await import("../studio");
setupDom();
const button = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find((element) =>
    element.textContent?.includes(text),
  ) ?? null;

async function solveDrag(position: [number, number, number]) {
  await act(async () => {
    scene.onTarget({
      position,
      quaternion: forwardPose(FR3_INITIAL_JOINTS).quaternion,
    });
    await new Promise((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(resolve)),
    );
  });
}

async function renderEditing() {
  const rendered = await render(<RobotStudio />);
  const actual = measured(0, 1);
  await act(async () => telemetry.onMeasurement(actual));
  await act(async () => telemetry.onUseMeasurement(actual));
  return rendered;
}

describe("Robot Studio interaction", () => {
  test("editing from fresh actual state saves and undoes solved poses", async () => {
    const { host } = await renderEditing();
    expect(host.textContent).toContain("Editing target");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(
      false,
    );
    await click(button(host, "Add pose"));
    expect(scene.waypoints).toHaveLength(1);
    await click(button(host, "Undo last"));
    expect(scene.waypoints).toHaveLength(0);
    expect((button(host, "Export JSON") as HTMLButtonElement).disabled).toBe(
      true,
    );
  });
  test("the link back goes to the marvel_workstation page (the old /marvel0 only redirects)", async () => {
    const { host } = await renderEditing();
    const link = host.querySelector<HTMLAnchorElement>("a.rs-link")!;
    expect(link.getAttribute("href")).toBe("/marvel_workstation");
    expect(link.textContent).toBe("Open marvel_workstation");
  });
  test("an unreachable drag keeps the accepted pose and blocks saving", async () => {
    const { host } = await renderEditing();
    const accepted = [...scene.joints];
    await solveDrag([1.8, 1.8, 1.8]);
    expect(scene.joints).toEqual(accepted);
    expect(scene.target.position).toEqual([1.8, 1.8, 1.8]);
    expect(host.textContent).toContain("No solution found");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
    await click(button(host, "Restore initial preview"));
    expect(scene.target).toEqual(forwardPose(FR3_INITIAL_JOINTS));
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(
      false,
    );
  });
  test("Escape restores the pose from the beginning of a drag", async () => {
    const { host } = await renderEditing();
    const target = scene.target;
    await act(async () => scene.onDragStart());
    await solveDrag([1.8, 1.8, 1.8]);
    await act(async () =>
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })),
    );
    expect(scene.target).toEqual(target);
    expect(scene.joints).toEqual([...FR3_INITIAL_JOINTS]);
    expect(host.textContent).toContain("Preview pose solved");
  });
  test("cancelling a drag that began from an unsolved target preserves the failure", async () => {
    const { host } = await renderEditing();
    await solveDrag([1.8, 1.8, 1.8]);
    await act(async () => scene.onDragStart());
    await solveDrag([1.7, 1.7, 1.7]);
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(scene.target.position).toEqual([1.8, 1.8, 1.8]);
    expect(host.textContent).toContain("No solution found");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
  });
  test("losing focus discards queued drag movement", async () => {
    await renderEditing();
    const target = scene.target;
    await act(async () => scene.onDragStart());
    await act(async () => {
      scene.onTarget({
        position: [1.8, 1.8, 1.8],
        quaternion: target.quaternion,
      });
      window.dispatchEvent(new Event("blur"));
      await new Promise((resolve) => requestAnimationFrame(resolve));
    });
    expect(scene.target).toEqual(target);
    expect(scene.joints).toEqual([...FR3_INITIAL_JOINTS]);
  });
  test("loading failure disables pose writes and exposes retry", async () => {
    const { host } = await renderEditing();
    await act(async () => scene.onLoad(false));
    expect(host.textContent).toContain("The 3D model could not load");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
    expect(scene.mode).toBe("view");
    expect(button(host, "Reload model")).not.toBeNull();
  });
});

test("reset and measured-copy obey the same applied box as dragging", async () => {
  const { host } = await renderEditing();
  const candidate = [...FR3_INITIAL_JOINTS];
  candidate[0] = 0.25;
  await act(async () =>
    telemetry.onUseMeasurement({
      joints_rad: candidate as [
        number,
        number,
        number,
        number,
        number,
        number,
        number,
      ],
      gripper: null,
      reported_ee: null,
    }),
  );
  const pose = forwardPose(candidate);
  await act(async () =>
    workspace.onChange({
      min: pose.position.map((x) => x - 0.01) as [number, number, number],
      max: pose.position.map((x) => x + 0.01) as [number, number, number],
    }),
  );
  const accepted = [...scene.joints];
  await click(button(host, "Restore initial preview"));
  expect(scene.joints).toEqual(accepted);
  expect(host.textContent).toContain("outside the draft box");
  await act(async () =>
    telemetry.onUseMeasurement({
      joints_rad: [...FR3_INITIAL_JOINTS],
      gripper: null,
      reported_ee: null,
    }),
  );
  expect(scene.joints).toEqual(accepted);
  expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
});

function measured(
  q0: number,
  ratio: number | null = 0.4,
): RobotStudioMeasurement {
  const joints = [
    ...FR3_INITIAL_JOINTS,
  ] as RobotStudioMeasurement["joints_rad"];
  joints[0] = q0;
  return {
    joints_rad: joints,
    gripper:
      ratio === null ? null : { opening_width_m: ROBOTIQ_STROKE_M * ratio },
    reported_ee: null,
  };
}

describe("measured initial alignment and follow/edit separation", () => {
  test("importing a boundary outside the target immediately revokes drag eligibility", async () => {
    const { host } = await renderEditing();
    expect(manualPanel.canDrag).toBe(true);
    await act(async () =>
      manualPanel.onConfigurationDraft?.(
        { min: [-1, -1, -1], max: [-0.5, 0.5, 0.5] },
        { linearSpeedMps: 0.02, positionStepMm: 1 },
      ),
    );
    expect(manualPanel.canDrag).toBe(false);
    expect(host.textContent).toContain("outside");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
  });
  test("viewport toolbar enters editing from measured state without starting a session", async () => {
    const { host } = await render(<RobotStudio />);
    const edit = button(host, "Edit target") as HTMLButtonElement;
    expect(edit.disabled).toBe(true);
    await act(async () => telemetry.onMeasurement(measured(0.25)));
    expect(edit.disabled).toBe(false);
    await click(edit);
    expect(telemetry.following).toBe(false);
    expect(scene.mode).toBe("translate");
    expect(manualPanel.canDrag).toBe(true);
    await click(button(host, "Follow measured state"));
    expect(scene.mode).toBe("view");
    expect(manualPanel.canDrag).toBe(false);
  });
  test("waits for feedback, aligns all joints and gripper, and freezes the last pose on expiry", async () => {
    const { host } = await render(<RobotStudio />);
    expect(telemetry.following).toBe(true);
    expect(scene.poseVisible).toBe(false);
    expect(manualPanel.canDrag).toBe(false);
    expect(scene.mode).toBe("view");
    expect(
      (host.querySelector('[aria-label="X (mm)"]') as HTMLInputElement).value,
    ).toBe("");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
    const first = measured(0.25);
    first.joints_rad[1] -= 0.05;
    first.joints_rad[2] += 0.05;
    first.joints_rad[3] += 0.05;
    first.joints_rad[4] += 0.05;
    first.joints_rad[5] += 0.05;
    first.joints_rad[6] += 0.05;
    await act(async () => telemetry.onMeasurement(first));
    expect(scene.joints).toEqual(first.joints_rad);
    expect(scene.target).toEqual(forwardPose(first.joints_rad));
    expect(scene.openingRatio).toBeCloseTo(0.4);
    expect(scene.toolVisible).toBe(true);
    expect(scene.poseVisible).toBe(true);
    expect(scene.measuredJoints).toBeNull();
    const next = measured(0.3, 0.6);
    await act(async () => telemetry.onMeasurement(next));
    expect(scene.joints).toEqual(next.joints_rad);
    expect(scene.openingRatio).toBeCloseTo(0.6);
    await act(async () => telemetry.onMeasurement(null));
    expect(scene.joints).toEqual(next.joints_rad);
    expect(scene.target).toEqual(forwardPose(next.joints_rad));
    expect(scene.poseVisible).toBe(true);
    expect(host.textContent).toContain("Last measured pose shown");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
  });
  test("editing starts at fresh measured joints and later feedback cannot overwrite the target", async () => {
    const { host } = await render(<RobotStudio />);
    const first = measured(0.25);
    await act(async () => telemetry.onEnabledChange(true));
    await act(async () => telemetry.onMeasurement(first));
    await act(async () => telemetry.onUseMeasurement(first));
    expect(telemetry.following).toBe(false);
    expect(scene.mode).toBe("translate");
    expect(scene.target).toEqual(forwardPose(first.joints_rad));
    const draftPose = scene.target;
    await act(async () => scene.onDragStart());
    const next = measured(0.35, 0.8);
    await act(async () => telemetry.onMeasurement(next));
    expect(scene.joints).toEqual(first.joints_rad);
    expect(scene.target).toEqual(draftPose);
    expect(scene.openingRatio).toBeCloseTo(0.4);
    expect(scene.measuredJoints).toEqual(next.joints_rad);
    expect(scene.measuredOpeningRatio).toBeCloseTo(0.8);
    await act(async () => scene.onDragEnd());
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(
      false,
    );
    await act(async () => telemetry.onFollowMeasurement(next));
    expect(telemetry.following).toBe(true);
    expect(scene.joints).toEqual(next.joints_rad);
    expect(scene.mode).toBe("view");
  });
  test("measured poses outside the box remain visible but cannot become an editable starting target", async () => {
    const { host } = await render(<RobotStudio />);
    const start = scene.target.position;
    await act(async () =>
      workspace.onChange({
        min: start.map((x) => x - 0.005) as [number, number, number],
        max: start.map((x) => x + 0.005) as [number, number, number],
      }),
    );
    const outside = measured(0.4);
    await act(async () => telemetry.onEnabledChange(true));
    await act(async () => telemetry.onMeasurement(outside));
    expect(scene.joints).toEqual(outside.joints_rad);
    expect(scene.target).toEqual(forwardPose(outside.joints_rad));
    expect(host.textContent).toContain("Measured flange is outside");
    await act(async () => telemetry.onUseMeasurement(outside));
    expect(telemetry.following).toBe(true);
    expect(scene.mode).toBe("view");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
  });
  test("unknown gripper feedback hides the measured tool, and stopping never restores illustrative joints", async () => {
    await render(<RobotStudio />);
    const actual = measured(0.25, null);
    await act(async () => telemetry.onEnabledChange(true));
    await act(async () => telemetry.onMeasurement(actual));
    expect(scene.poseVisible).toBe(true);
    expect(scene.toolVisible).toBe(false);
    await act(async () => telemetry.onEnabledChange(false));
    expect(scene.joints).toEqual(actual.joints_rad);
    expect(scene.target).toEqual(forwardPose(actual.joints_rad));
    expect(scene.mode).toBe("view");
    await act(async () => telemetry.onEnabledChange(true));
    expect(scene.poseVisible).toBe(true);
  });
  test("enabling follow discards a queued drag before accepting the measured start", async () => {
    await render(<RobotStudio />);
    await act(async () => scene.onDragStart());
    const actual = measured(0.3);
    await act(async () => {
      scene.onTarget({
        position: [1.8, 1.8, 1.8],
        quaternion: scene.target.quaternion,
      });
      telemetry.onEnabledChange(true);
      telemetry.onMeasurement(actual);
      await new Promise((resolve) => requestAnimationFrame(resolve));
    });
    expect(scene.joints).toEqual(actual.joints_rad);
    expect(scene.target).toEqual(forwardPose(actual.joints_rad));
    expect(scene.mode).toBe("view");
  });
});

test("returning to follow after expiry freezes the latest measured edit overlay", async () => {
  const { host } = await render(<RobotStudio />);
  const first = measured(0.25);
  await act(async () => telemetry.onEnabledChange(true));
  await act(async () => telemetry.onMeasurement(first));
  await act(async () => telemetry.onUseMeasurement(first));
  const latest = measured(0.35, 0.6);
  await act(async () => telemetry.onMeasurement(latest));
  expect(scene.joints).toEqual(first.joints_rad);
  await act(async () => telemetry.onMeasurement(null));
  await act(async () => telemetry.onFollowMeasurement(null));
  expect(scene.poseVisible).toBe(true);
  expect(scene.joints).toEqual(latest.joints_rad);
  expect(scene.target).toEqual(forwardPose(latest.joints_rad));
  expect(scene.openingRatio).toBeCloseTo(0.6);
  expect(scene.mode).toBe("view");
  expect(host.textContent).toContain("Last measured pose shown");
  expect(host.textContent).not.toContain(
    "Waiting for the first fresh measured pose",
  );
});

describe("manual drag integration", () => {
  test("the server joint anchor aligns the model before a manual drag", async () => {
    await renderEditing();
    const joints = [...FR3_INITIAL_JOINTS];
    joints[0] += 0.05;
    const pose = forwardPose(joints);
    await act(async () =>
      manualPanel.onSessionAnchor({
        position: pose.position,
        quaternion: pose.quaternion,
        joints_rad: joints as [
          number,
          number,
          number,
          number,
          number,
          number,
          number,
        ],
        sampled_at_mono: 1,
      }),
    );
    scene.joints.forEach((q, i) => expect(q).toBeCloseTo(joints[i], 7));
    expect(scene.target.position).toEqual(pose.position);
  });
  test("only solved targets reach the bridge and drag end revokes it", async () => {
    await renderEditing();
    manualBridge.beginDrag.mockClear();
    manualBridge.target.mockClear();
    manualBridge.revoke.mockClear();
    await act(async () => scene.onDragStart());
    expect(manualBridge.beginDrag).toHaveBeenCalledTimes(1);
    await solveDrag(forwardPose(FR3_INITIAL_JOINTS).position);
    expect(manualBridge.target).toHaveBeenCalledTimes(1);
    await solveDrag([1.8, 1.8, 1.8]);
    expect(manualBridge.target).toHaveBeenCalledTimes(1);
    expect(manualBridge.revoke).toHaveBeenCalledWith("preview_invalid");
    await act(async () => scene.onDragEnd());
    expect(manualBridge.revoke).toHaveBeenCalledWith("drag_end");
  });
  test("release discards a queued RAF target instead of sending a final goal", async () => {
    await renderEditing();
    manualBridge.target.mockClear();
    await act(async () => {
      scene.onDragStart();
      scene.onTarget(forwardPose(FR3_INITIAL_JOINTS));
      scene.onDragEnd();
      await new Promise((resolve) => requestAnimationFrame(resolve));
    });
    expect(manualBridge.target).not.toHaveBeenCalled();
  });
});

test("a nonfinite 3D target with a workspace box revokes without sending", async () => {
  await renderEditing();
  await act(async () =>
    workspace.onChange({ min: [-1, -1, 0], max: [1, 1, 1] }),
  );
  manualBridge.target.mockClear();
  manualBridge.revoke.mockClear();
  await act(async () => scene.onDragStart());
  await solveDrag([Number.NaN, 0, 0]);
  expect(manualBridge.target).not.toHaveBeenCalled();
  expect(manualBridge.revoke).toHaveBeenCalledWith("preview_invalid");
});

test("feedback loss while editing freezes actual ghost, cancels motion and blocks drag", async () => {
  const { host } = await renderEditing();
  const latest = measured(0.35, 0.6);
  await act(async () => telemetry.onMeasurement(latest));
  const draft = scene.target;
  manualBridge.beginDrag.mockClear();
  manualBridge.target.mockClear();
  manualBridge.revoke.mockClear();
  await act(async () => telemetry.onMeasurement(null));
  expect(scene.measuredJoints).toEqual(latest.joints_rad);
  expect(scene.measuredOpeningRatio).toBeCloseTo(0.6);
  expect(scene.target).toEqual(draft);
  expect(scene.mode).toBe("view");
  expect(manualPanel.canDrag).toBe(false);
  expect(manualBridge.revoke).toHaveBeenCalledWith("feedback_stale");
  await act(async () => scene.onDragStart());
  await solveDrag([1.8, 1.8, 1.8]);
  expect(manualBridge.beginDrag).not.toHaveBeenCalled();
  expect(manualBridge.target).not.toHaveBeenCalled();
  expect(scene.target).toEqual(draft);
  expect(host.textContent).toContain("Last measured pose shown");
});

test("default follow ignores reported Desk EE offset when mapping model flange", async () => {
  await render(<RobotStudio />);
  const actual = measured(0.15, 0.25);
  actual.reported_ee = {
    position_m: [9, 8, 7],
    quaternion_xyzw: [0, 0, 0, 1],
    frame_id: "base",
    end_frame: "robot_server_desk_EE",
  };
  manualBridge.beginDrag.mockClear();
  manualBridge.target.mockClear();
  await act(async () => telemetry.onMeasurement(actual));
  expect(scene.joints).toEqual(actual.joints_rad);
  expect(scene.target).toEqual(forwardPose(actual.joints_rad));
  expect(scene.openingRatio).toBeCloseTo(0.25);
  expect(manualPanel.canDrag).toBe(false);
  await act(async () => scene.onDragStart());
  expect(manualBridge.beginDrag).not.toHaveBeenCalled();
  expect(manualBridge.target).not.toHaveBeenCalled();
});

test("unknown measured gripper stays hidden when entering target edit", async () => {
  const { host } = await render(<RobotStudio />);
  const actual = measured(0.25, null);
  await act(async () => telemetry.onMeasurement(actual));
  expect(scene.toolVisible).toBe(false);
  expect(
    host.querySelector('[data-testid="tool-mount-status"]')?.textContent,
  ).toContain("not calibrated");
  await act(async () => telemetry.onUseMeasurement(actual));
  expect(telemetry.following).toBe(false);
  expect(scene.toolVisible).toBe(false);
  // Merely receiving an arm-only sample while editing cannot invent an opening.
  await act(async () => telemetry.onMeasurement(measured(0.3, null)));
  expect(scene.toolVisible).toBe(false);
  expect(scene.measuredOpeningRatio).toBeNull();
  await act(async () => toolPanel.onOpeningRatio(0.4));
  expect(scene.toolVisible).toBe(true);
  expect(scene.openingRatio).toBeCloseTo(0.4);
  // Explicit preview editing does not manufacture actual gripper feedback.
  expect(scene.measuredOpeningRatio).toBeNull();
  await act(async () =>
    toolPanel.onDraft({
      mount: { position: [0, 0, 0], quaternion: [0, 0, 0, 1] },
      tcp: null,
    }),
  );
  await act(async () => telemetry.onFollowMeasurement(actual));
  expect(
    host.querySelector('[data-testid="tool-mount-status"]')?.textContent,
  ).toContain("unverified draft");
  expect(scene.toolVisible).toBe(false);
});
