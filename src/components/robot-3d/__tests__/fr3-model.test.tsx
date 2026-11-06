import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act, createRef } from "react";
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import * as THREE from "three";
import { FR3_JOINT_NAMES } from "@/lib/robot-studio/fr3-kinematics";
import { mediaColor } from "@/components/viewer/data-palette";

// The URDF loader reads the network; the fake builds a bare group that records
// every setJointValue and finishes through the LoadingManager like the real one.
const invalidate = mock(() => {});
let jointCalls: Array<[string, number]> = [];
let models: THREE.Group[] = [];
// A load that waits for the test, to look at the arm before it exists.
let holdLoads = false;
let held: Array<() => void> = [];
class FakeURDFLoader {
  loadMeshCb: unknown = null;
  constructor(private manager: THREE.LoadingManager) {}
  load(_url: string, onLoad: (model: THREE.Group) => void) {
    const model = Object.assign(new THREE.Group(), {
      setJointValue(name: string, value: number) {
        jointCalls.push([name, value]);
      },
    });
    model.add(
      new THREE.Mesh(
        new THREE.BoxGeometry(1, 1, 1),
        new THREE.MeshStandardMaterial({ color: "#ffffff" }),
      ),
    );
    models.push(model);
    const finish = () => {
      onLoad(model);
      queueMicrotask(() => this.manager.onLoad?.());
    };
    if (holdLoads) held.push(finish);
    else finish();
  }
}
mock.module("urdf-loader", () => ({ default: FakeURDFLoader }));
mock.module("@react-three/fiber", () => ({ useThree: () => ({ invalidate }) }));

const { Fr3Robot, tintGhost, FR3_CANVAS_PROPS } = await import("../fr3-model");
setupDom();

const consoleError = console.error;
beforeEach(() => {
  jointCalls = [];
  models = [];
  holdLoads = false;
  held = [];
  invalidate.mockClear();
  // <primitive> is a three element; react-dom does not know it.
  console.error = () => {};
});
afterEach(() => {
  console.error = consoleError;
});

const flushLoad = () =>
  act(async () => {
    for (const finish of held.splice(0)) finish();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
const lastJoint = (name: string) =>
  jointCalls.filter(([joint]) => joint === name).at(-1)?.[1];

describe("Fr3Robot (shared FR3 model)", () => {
  test("a load poses the arm from the joints array as it is at that moment", async () => {
    holdLoads = true;
    const joints = new Float64Array(7);
    const onLoad = mock((ok: boolean) => void ok);
    await render(<Fr3Robot joints={joints} onLoad={onLoad} />);
    expect(onLoad).not.toHaveBeenCalled();
    // Mutated in place after mount and before the load finishes, as a replay does.
    joints.set([0.1, -0.2, 0.3, -1.5, 0.5, 1.2, 0.7]);
    await flushLoad();
    expect(onLoad).toHaveBeenCalledWith(true);
    FR3_JOINT_NAMES.forEach((name, i) =>
      expect(lastJoint(name)).toBeCloseTo(joints[i], 12),
    );
  });

  test("setJoints poses the loaded arm and asks for one redraw; before the load it does nothing", async () => {
    holdLoads = true;
    const ref = createRef<{ setJoints: (q: ArrayLike<number>) => void }>();
    await render(
      <Fr3Robot ref={ref} joints={new Float64Array(7)} onLoad={() => {}} />,
    );
    ref.current!.setJoints([1, 1, 1, 1, 1, 1, 1]);
    expect(jointCalls.filter(([, value]) => value === 1)).toEqual([]);
    await flushLoad();
    invalidate.mockClear();
    ref.current!.setJoints([0.5, -0.5, 0.25, -2, 0.1, 1.5, -0.3]);
    expect(lastJoint(FR3_JOINT_NAMES[3])).toBe(-2);
    expect(lastJoint(FR3_JOINT_NAMES[6])).toBeCloseTo(-0.3, 12);
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  test("ghost tints the model translucent in its own colour", async () => {
    await render(
      <Fr3Robot ghost joints={new Float64Array(7)} onLoad={() => {}} />,
    );
    await flushLoad();
    const material = (models[0].children[0] as THREE.Mesh)
      .material as THREE.MeshStandardMaterial;
    expect(material.transparent).toBe(true);
    expect(material.opacity).toBe(0.35);
    expect(material.depthWrite).toBe(false);
    expect(material.color.getHexString()).toBe(
      new THREE.Color(mediaColor(1)).getHexString(),
    );
  });

  test("a plain model keeps its own opaque materials", async () => {
    await render(<Fr3Robot joints={new Float64Array(7)} onLoad={() => {}} />);
    await flushLoad();
    const material = (models[0].children[0] as THREE.Mesh)
      .material as THREE.MeshStandardMaterial;
    expect(material.transparent).toBe(false);
    expect(material.opacity).toBe(1);
  });

  test("decorate runs once on the loaded model (the studio's measured tint goes through it)", async () => {
    const decorate = mock((model: unknown) => void model);
    await render(
      <Fr3Robot
        decorate={decorate}
        joints={new Float64Array(7)}
        onLoad={() => {}}
      />,
    );
    await flushLoad();
    expect(decorate).toHaveBeenCalledTimes(1);
    expect(decorate.mock.calls[0][0]).toBe(models[0]);
  });
});

describe("shared settings", () => {
  test("tintGhost leaves non-mesh nodes alone", () => {
    const group = new THREE.Group();
    group.add(new THREE.Object3D());
    expect(() => tintGhost(group)).not.toThrow();
  });
  test("the Canvas settings are the studio's: demand frames, z-up camera", () => {
    expect(FR3_CANVAS_PROPS.frameloop).toBe("demand");
    expect(FR3_CANVAS_PROPS.dpr).toEqual([1, 1.5]);
    expect(FR3_CANVAS_PROPS.camera).toMatchObject({
      position: [1.35, 1.35, 1.05],
      up: [0, 0, 1],
      fov: 40,
    });
  });
});
