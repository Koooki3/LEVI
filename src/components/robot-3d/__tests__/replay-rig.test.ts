import { describe, expect, test } from "bun:test";
import * as THREE from "three";
import { detectFr3Replay } from "@/lib/replay/fr3-columns";
import { buildReplayData } from "@/lib/replay/frames";
import { quaternionFromRpy } from "@/lib/replay/pose";
import { MAX_TRAIL_POINTS, createReplaySampler } from "@/lib/replay/sampler";
import { createReplayRig } from "../replay-rig";

const names = ["x", "y", "z", "rx", "ry", "rz"];
const keys = (feature: string) => names.map((n) => `${feature} | ${n}`);
function setup(n: number, withAction = false) {
  const rows = Array.from({ length: n }, (_, i) => ({
    timestamp: i,
    "observation.state | x": i / 100,
    "observation.state | y": 0.5,
    "observation.state | z": 0.25,
    "observation.state | rx": Math.PI / 2,
    "observation.state | ry": 0,
    "observation.state | rz": 0.1,
    "action | x": 1 + i / 100,
    "action | y": 0,
    "action | z": 0.5,
    "action | rx": 0,
    "action | ry": 0,
    "action | rz": 0,
  }));
  const flat = [
    "timestamp",
    ...keys("observation.state"),
    ...(withAction ? keys("action") : []),
  ];
  const data = buildReplayData(
    detectFr3Replay({ robot_type: "generic_arm" }, flat),
    rows,
  )!;
  const sampler = createReplaySampler(data);
  return { data, sampler, rig: createReplayRig(data, sampler) };
}
const find = (group: THREE.Group, type: string) =>
  group.children.filter((child) => child.type === type);

describe("replay rig (pose mode)", () => {
  test("the trail is hidden until asked for, and draws nothing then", () => {
    const { sampler, rig } = setup(50);
    const frame = sampler.at(10);
    rig.update(frame, { showAction: false, trail: false });
    const [trail] = find(rig.group, "Line");
    expect(trail.visible).toBe(false);
    expect(rig.trailPoints()).toBe(0);
  });

  test("the trail is the rows before the current one plus the live position", () => {
    const { sampler, rig } = setup(50);
    const frame = sampler.at(10.5);
    rig.update(frame, { showAction: false, trail: true });
    expect(rig.trailPoints()).toBe(11); // rows 0..9 and the live point
    const [trail] = find(rig.group, "Line") as THREE.Line[];
    expect(trail.visible).toBe(true);
    const positions = trail.geometry.getAttribute("position")
      .array as Float32Array;
    expect(positions[3 * 9]).toBeCloseTo(0.09, 6);
    expect(positions[3 * 10]).toBeCloseTo(0.105, 6); // the live x, between rows 10 and 11
  });

  test("the trail never has more than MAX_TRAIL_POINTS points, however long the episode", () => {
    const { sampler, rig } = setup(2000);
    rig.update(sampler.at(1500), { showAction: false, trail: true });
    expect(rig.trailPoints()).toBe(MAX_TRAIL_POINTS);
    rig.update(sampler.at(1999), { showAction: false, trail: true });
    expect(rig.trailPoints()).toBeLessThanOrEqual(MAX_TRAIL_POINTS);
    const [trail] = find(rig.group, "Line") as THREE.Line[];
    expect(
      (trail.geometry.getAttribute("position").array as Float32Array).length,
    ).toBe(MAX_TRAIL_POINTS * 3);
  });

  test("seeking back shortens the trail instead of leaving points ahead of the time", () => {
    const { sampler, rig } = setup(100);
    rig.update(sampler.at(80), { showAction: false, trail: true });
    expect(rig.trailPoints()).toBe(81);
    rig.update(sampler.at(5), { showAction: false, trail: true });
    expect(rig.trailPoints()).toBe(6);
  });

  test("the axes sit at the pose, turned by the raw.py roll/pitch/yaw rotation", () => {
    const { sampler, rig } = setup(10);
    rig.update(sampler.at(3), { showAction: false, trail: false });
    const marker = rig.group.children.find(
      (child) => child.type === "Group",
    ) as THREE.Group;
    expect(marker.position.x).toBeCloseTo(0.03, 6);
    expect(marker.position.y).toBeCloseTo(0.5, 6);
    const expected = quaternionFromRpy(Math.PI / 2, 0, 0.1);
    const q = marker.quaternion;
    const sign =
      q.x * expected[0] +
        q.y * expected[1] +
        q.z * expected[2] +
        q.w * expected[3] <
      0
        ? -1
        : 1;
    expect(q.x * sign).toBeCloseTo(expected[0], 9);
    expect(q.y * sign).toBeCloseTo(expected[1], 9);
    expect(q.z * sign).toBeCloseTo(expected[2], 9);
    expect(q.w * sign).toBeCloseTo(expected[3], 9);
  });

  test("the whole trajectory is drawn once, dim, one point per row", () => {
    const { rig } = setup(40);
    const lines = find(rig.group, "Line") as THREE.Line[];
    const path = lines.find(
      (line) =>
        line.material instanceof THREE.LineBasicMaterial &&
        line.material.transparent,
    )!;
    expect(path.geometry.getAttribute("position").count).toBe(40);
  });

  test("the action's axes show only with showAction, at the action pose", () => {
    const { sampler, rig } = setup(10, true);
    const groups = () =>
      rig.group.children.filter((child) => child.type === "Group");
    const action = groups()[1];
    rig.update(sampler.at(3), { showAction: false, trail: false });
    expect(action.visible).toBe(false);
    rig.update(sampler.at(3), { showAction: true, trail: false });
    expect(action.visible).toBe(true);
    expect(action.position.x).toBeCloseTo(1.03, 6);
    expect(action.position.z).toBeCloseTo(0.5, 6);
  });

  test("no action columns: no second set of axes at all", () => {
    const { rig } = setup(10, false);
    expect(
      rig.group.children.filter((child) => child.type === "Group").length,
    ).toBe(1);
  });

  test("dispose releases geometry and materials and can be called twice", () => {
    const { rig } = setup(10, true);
    expect(() => {
      rig.dispose();
      rig.dispose();
    }).not.toThrow();
  });
});
