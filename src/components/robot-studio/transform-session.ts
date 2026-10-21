import { type Camera, type Object3D, type Scene } from "three";
import { TransformControls } from "three-stdlib";
import type { PreviewPose } from "./preview-state";

// three-stdlib dispatches these documented events but its Object3D declaration
// does not include them in the inherited event map.
type EventType = "change" | "objectChange" | "mouseDown" | "mouseUp";
type ControlEvents = {
  addEventListener: (type: EventType, callback: () => void) => void;
  removeEventListener: (type: EventType, callback: () => void) => void;
};
export type TargetCallbacks = {
  onTarget: (target: PreviewPose) => void;
  onStart: () => void;
  onEnd: () => void;
};

/** A single pointer session owner. Disposal removes document listeners too. */
export function bindTargetControls(options: {
  camera: Camera;
  element: HTMLElement;
  scene: Scene;
  target: Object3D;
  mode: "translate" | "rotate";
  space: "world" | "local";
  callbacks: () => TargetCallbacks;
  invalidate: () => void;
}): () => void {
  const controls = new TransformControls(options.camera, options.element);
  const events = controls as unknown as ControlEvents;
  controls.attach(options.target);
  controls.setMode(options.mode);
  controls.setSpace(options.space);
  controls.setSize(0.85);
  options.scene.add(controls);
  let active = true;
  let dragging = false;
  const start = () => {
    if (active) {
      dragging = true;
      options.callbacks().onStart();
    }
  };
  const end = () => {
    if (active && dragging) {
      dragging = false;
      options.callbacks().onEnd();
    }
  };
  const change = () => {
    if (!active) return;
    const p = options.target.position;
    const q = options.target.quaternion;
    options.callbacks().onTarget({
      position: [p.x, p.y, p.z],
      quaternion: [q.x, q.y, q.z, q.w],
    });
  };
  events.addEventListener("mouseDown", start);
  events.addEventListener("mouseUp", end);
  events.addEventListener("objectChange", change);
  events.addEventListener("change", options.invalidate);
  options.invalidate();
  return () => {
    if (!active) return;
    active = false;
    events.removeEventListener("mouseDown", start);
    events.removeEventListener("mouseUp", end);
    events.removeEventListener("objectChange", change);
    events.removeEventListener("change", options.invalidate);
    // reset() alone leaves dragging and document pointermove live. Remove the
    // old control entirely before mounting a new one after Escape/blur.
    controls.dispose();
    controls.detach();
    options.scene.remove(controls);
    if (dragging) options.callbacks().onEnd();
    options.invalidate();
  };
}
