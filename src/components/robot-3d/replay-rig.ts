// The replay's own three.js objects, besides the arm: the end-effector trail,
// and in pose mode the whole trajectory with coordinate axes at the current
// pose (and at the action's). Plain three, no React and no WebGL: the scene
// hangs `group` in the Canvas and calls `update` with each new frame, which
// writes into buffers made once here. Colours come from the data palette.
import * as THREE from "three";
import { mediaColor } from "@/components/viewer/data-palette";
import type { ReplayData } from "@/lib/replay/frames";
import {
  MAX_TRAIL_POINTS,
  type ReplayFrame,
  type ReplaySampler,
} from "@/lib/replay/sampler";

export interface ReplayRigFlags {
  showAction: boolean;
  trail: boolean;
}

export interface ReplayRig {
  group: THREE.Group;
  update(frame: ReplayFrame, flags: ReplayRigFlags): void;
  /** Number of points the trail currently draws (for tests and the text readout). */
  trailPoints(): number;
  dispose(): void;
}

const STATE_COLOR = mediaColor(0);
const ACTION_COLOR = mediaColor(1);

function fade(material: THREE.Material, opacity: number) {
  material.transparent = true;
  material.opacity = opacity;
  material.depthWrite = false;
}

function axesMarker(size: number, dot: string, opacity: number) {
  const marker = new THREE.Group();
  const axes = new THREE.AxesHelper(size);
  const dotMaterial = new THREE.MeshBasicMaterial({ color: dot });
  marker.add(axes);
  marker.add(
    new THREE.Mesh(new THREE.SphereGeometry(size / 10, 12, 8), dotMaterial),
  );
  if (opacity < 1) {
    fade(axes.material as THREE.Material, opacity);
    fade(dotMaterial, opacity);
  }
  return marker;
}

export function createReplayRig(
  data: ReplayData,
  sampler: ReplaySampler,
): ReplayRig {
  const group = new THREE.Group();
  const geometries: THREE.BufferGeometry[] = [];
  const materials: THREE.Material[] = [];

  const trailAttribute = new THREE.BufferAttribute(
    new Float32Array(MAX_TRAIL_POINTS * 3),
    3,
  );
  trailAttribute.setUsage(THREE.DynamicDrawUsage);
  const trailGeometry = new THREE.BufferGeometry();
  trailGeometry.setAttribute("position", trailAttribute);
  trailGeometry.setDrawRange(0, 0);
  const trailMaterial = new THREE.LineBasicMaterial({ color: STATE_COLOR });
  const trailLine = new THREE.Line(trailGeometry, trailMaterial);
  // Its bounds are never computed (the geometry changes every frame).
  trailLine.frustumCulled = false;
  trailLine.visible = false;
  group.add(trailLine);
  geometries.push(trailGeometry);
  materials.push(trailMaterial);

  let stateMarker: THREE.Group | null = null;
  let actionMarker: THREE.Group | null = null;
  if (data.mode === "pose") {
    // The whole trajectory, dim; the trail on top of it is the recent part.
    const pathGeometry = new THREE.BufferGeometry();
    pathGeometry.setAttribute(
      "position",
      new THREE.BufferAttribute(sampler.path(), 3),
    );
    const pathMaterial = new THREE.LineBasicMaterial({ color: STATE_COLOR });
    fade(pathMaterial, 0.35);
    const pathLine = new THREE.Line(pathGeometry, pathMaterial);
    pathLine.frustumCulled = false;
    group.add(pathLine);
    geometries.push(pathGeometry);
    materials.push(pathMaterial);
    stateMarker = axesMarker(0.08, STATE_COLOR, 1);
    group.add(stateMarker);
  }
  if (data.actionMode === "pose") {
    actionMarker = axesMarker(0.06, ACTION_COLOR, 0.5);
    actionMarker.visible = false;
    group.add(actionMarker);
  }

  let drawn = 0;
  const writeTrail = (frame: ReplayFrame) => {
    const { from, to } = sampler.trailRange(frame.index);
    const path = sampler.path();
    const out = trailAttribute.array as Float32Array;
    // Rows before the current one, then the live position: at most MAX points.
    const rows = Math.max(0, to - from - 1);
    out.set(path.subarray(from * 3, (from + rows) * 3), 0);
    out[rows * 3] = frame.position[0];
    out[rows * 3 + 1] = frame.position[1];
    out[rows * 3 + 2] = frame.position[2];
    drawn = rows + 1;
    trailGeometry.setDrawRange(0, drawn);
    trailAttribute.needsUpdate = true;
  };

  return {
    group,
    update(frame, flags) {
      trailLine.visible = flags.trail;
      if (flags.trail) writeTrail(frame);
      if (stateMarker) {
        stateMarker.position.fromArray(frame.position);
        stateMarker.quaternion.fromArray(frame.quaternion);
      }
      if (actionMarker) {
        actionMarker.visible = flags.showAction;
        actionMarker.position.fromArray(frame.actionPosition);
        actionMarker.quaternion.fromArray(frame.actionQuaternion);
      }
    },
    trailPoints: () => drawn,
    dispose() {
      group.traverse((child) => {
        if (
          child instanceof THREE.Mesh ||
          child instanceof THREE.LineSegments
        ) {
          child.geometry.dispose();
          const list = Array.isArray(child.material)
            ? child.material
            : [child.material];
          for (const material of list) material.dispose();
        }
      });
      for (const geometry of geometries) geometry.dispose();
      for (const material of materials) material.dispose();
    },
  };
}
