import { setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { Object3D, PerspectiveCamera, Scene } from "three";
import { bindTargetControls } from "../transform-session";
setupDom();

describe("real TransformControls pointer lifecycle", () => {
  test("disposal removes a held pointer session and suppresses stale changes", () => {
    const scene = new Scene();
    const target = new Object3D();
    scene.add(target);
    const camera = new PerspectiveCamera(40, 1, 0.01, 20);
    camera.position.set(2, 2, 2);
    camera.lookAt(0, 0, 0);
    camera.updateMatrixWorld();
    const element = document.createElement("canvas");
    element.getBoundingClientRect = () => ({
      x: 0,
      y: 0,
      width: 400,
      height: 400,
      left: 0,
      top: 0,
      right: 400,
      bottom: 400,
      toJSON: () => ({}),
    });
    document.body.appendChild(element);
    const onTarget = mock(() => {}),
      onStart = mock(() => {}),
      onEnd = mock(() => {});
    const dispose = bindTargetControls({
      camera,
      element,
      scene,
      target,
      mode: "translate",
      space: "world",
      callbacks: () => ({ onTarget, onStart, onEnd }),
      invalidate: () => {},
    });
    const control = scene.children.find((child) => child !== target)!;
    // Runtime fields are private only in the upstream TS declaration. Arrange
    // a held pointer; dispatch real DOM moves before/after removing the control.
    const runtime = control as unknown as {
      axis: string;
      dragging: boolean;
      dispatchEvent: (event: { type: string }) => void;
    };
    runtime.axis = "X";
    scene.updateMatrixWorld(true);
    element.dispatchEvent(
      new PointerEvent("pointerdown", {
        clientX: 200,
        clientY: 200,
        button: 0,
        pointerId: 1,
      }),
    );
    runtime.dragging = true;
    runtime.dispatchEvent({ type: "mouseDown" });
    document.dispatchEvent(
      new PointerEvent("pointermove", {
        clientX: 220,
        clientY: 200,
        button: -1,
        pointerId: 1,
      }),
    );
    expect(onStart.mock.calls.length).toBeGreaterThan(0);
    expect(onTarget.mock.calls.length).toBeGreaterThan(0);
    dispose();
    const accepted = target.position.clone();
    onTarget.mockClear();
    document.dispatchEvent(
      new PointerEvent("pointermove", {
        clientX: 300,
        clientY: 200,
        button: -1,
        pointerId: 1,
      }),
    );
    runtime.dispatchEvent({ type: "objectChange" });
    expect(target.position.toArray()).toEqual(accepted.toArray());
    expect(onTarget).not.toHaveBeenCalled();
    expect(onEnd).toHaveBeenCalledTimes(1);
    expect(scene.children).toEqual([target]);
    dispose();
    expect(onEnd).toHaveBeenCalledTimes(1);
    element.remove();
  });
});
