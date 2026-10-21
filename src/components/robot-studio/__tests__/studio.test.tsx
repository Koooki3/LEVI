import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { act, useEffect, type ComponentProps } from "react";
import { describe, expect, mock, test } from "bun:test";
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

describe("Robot Studio interaction", () => {
  test("renders an explicit offline workspace, saves and undoes solved poses", async () => {
    const { host } = await render(<RobotStudio />);
    expect(host.textContent).toContain("Offline preview");
    expect(host.textContent).toContain("not connected to a robot");
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
  test("an unreachable drag keeps the accepted pose and blocks saving", async () => {
    const { host } = await render(<RobotStudio />);
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
    const { host } = await render(<RobotStudio />);
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
    const { host } = await render(<RobotStudio />);
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
    await render(<RobotStudio />);
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
    const { host } = await render(<RobotStudio />);
    await act(async () => scene.onLoad(false));
    expect(host.textContent).toContain("The 3D model could not load");
    expect((button(host, "Add pose") as HTMLButtonElement).disabled).toBe(true);
    expect(scene.mode).toBe("view");
    expect(button(host, "Reload model")).not.toBeNull();
  });
});
