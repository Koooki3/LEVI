"use client";
import { useEffect, useRef, useState } from "react";
import { useThree } from "@react-three/fiber";
import * as THREE from "three";
import URDFLoader, { type URDFRobot } from "urdf-loader";
import { robotGlbLoader } from "@/lib/robot-studio/glb-loader";
import { forwardPose } from "@/lib/robot-studio/fr3-kinematics";
import {
  ROBOTIQ_MODEL_URL,
  ROBOTIQ_DRIVE_JOINT,
  openingRatioToAngle,
} from "@/lib/robot-studio/robotiq-2f85";
import type { ToolDraft } from "./tool-draft";
import type { PreviewPose } from "./preview-state";
import { mediaColor } from "@/components/viewer/data-palette";

const IDENTITY: PreviewPose = { position: [0, 0, 0], quaternion: [0, 0, 0, 1] };

export function tintMeasured(object: THREE.Object3D) {
  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    for (const material of Array.isArray(child.material)
      ? child.material
      : [child.material]) {
      if ("color" in material && material.color instanceof THREE.Color)
        material.color.set(mediaColor(0));
      material.transparent = true;
      material.opacity = 0.28;
      material.depthWrite = false;
    }
  });
}

function dispose(object: THREE.Object3D) {
  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    child.geometry.dispose();
    for (const material of Array.isArray(child.material)
      ? child.material
      : [child.material])
      material.dispose();
  });
}

export default function ToolModel({
  joints,
  openingRatio,
  draft,
  onLoad,
  measured = false,
}: {
  joints: number[];
  openingRatio: number;
  draft: ToolDraft;
  onLoad: (ready: boolean) => void;
  measured?: boolean;
}) {
  const [robot, setRobot] = useState<URDFRobot | null>(null);
  const currentOpening = useRef(openingRatio);
  currentOpening.current = openingRatio;
  const { invalidate } = useThree();
  useEffect(() => {
    let cancelled = false;
    let failed = false;
    let loaded: URDFRobot | null = null;
    const manager = new THREE.LoadingManager();
    manager.onError = () => {
      failed = true;
      if (!cancelled) onLoad(false);
    };
    manager.onLoad = () =>
      queueMicrotask(() => {
        if (cancelled) {
          if (loaded) dispose(loaded);
          return;
        }
        if (!loaded || failed) return;
        loaded.setJointValue(
          ROBOTIQ_DRIVE_JOINT,
          openingRatioToAngle(currentOpening.current),
        );
        if (measured) tintMeasured(loaded);
        setRobot(loaded);
        onLoad(true);
        invalidate();
      });
    const loader = new URDFLoader(manager);
    loader.loadMeshCb = (path, resourceManager, done) => {
      robotGlbLoader(resourceManager).load(
        path,
        (model) => done(model.scene),
        undefined,
        (error) => done(new THREE.Group(), error as Error),
      );
    };
    loader.load(
      ROBOTIQ_MODEL_URL,
      (model) => {
        loaded = model;
      },
      undefined,
      () => {
        failed = true;
        if (!cancelled) onLoad(false);
      },
    );
    return () => {
      cancelled = true;
      if (loaded) dispose(loaded);
    };
  }, [invalidate, onLoad, measured]);
  useEffect(() => {
    if (!robot) return;
    robot.setJointValue(ROBOTIQ_DRIVE_JOINT, openingRatioToAngle(openingRatio));
    robot.updateMatrixWorld(true);
    invalidate();
  }, [robot, openingRatio, invalidate]);
  const flange = forwardPose(joints);
  const mount = draft.mount ?? IDENTITY;
  return (
    <group position={flange.position} quaternion={flange.quaternion}>
      <group position={mount.position} quaternion={mount.quaternion}>
        {robot && <primitive object={robot} />}
        {!measured && draft.mount && draft.tcp && (
          <group
            position={draft.tcp.position}
            quaternion={draft.tcp.quaternion}
          >
            <axesHelper args={[0.075]} />
            <mesh>
              <sphereGeometry args={[0.005, 10, 8]} />
              <meshBasicMaterial color={mediaColor(3)} />
            </mesh>
          </group>
        )}
      </group>
    </group>
  );
}
