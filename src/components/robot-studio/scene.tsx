"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import { bindTargetControls, type TargetCallbacks } from "./transform-session";
import { mediaColor } from "@/components/viewer/data-palette";
import {
  FR3_CANVAS_PROPS,
  Fr3Backdrop,
  Fr3CameraView,
  Fr3Robot,
} from "@/components/robot-3d/fr3-model";
import type { PreviewPose, Waypoint } from "./preview-state";
import ToolModel, { tintMeasured } from "./tool-model";
import type { ToolDraft } from "./tool-draft";
import type { WorkspaceBox } from "./workspace-draft";
const IGNORE_LOAD = () => {};

export type StudioMode = "view" | "translate" | "rotate";
type Props = {
  joints: number[];
  poseVisible: boolean;
  toolVisible: boolean;
  openingRatio: number;
  toolDraft: ToolDraft;
  measuredJoints: number[] | null;
  measuredOpeningRatio: number | null;
  workspaceBox: WorkspaceBox | null;
  target: PreviewPose;
  mode: StudioMode;
  space: "world" | "local";
  view: "perspective" | "front" | "top";
  viewRevision: number;
  cancelRevision: number;
  waypoints: Waypoint[];
  onTarget: (pose: PreviewPose) => void;
  onDragStart: () => void;
  onDragEnd: () => void;
  onLoad: (ok: boolean) => void;
};

// The loader, tint plumbing and joint posing live in the shared FR3 model
// (`robot-3d/fr3-model.tsx`); `measured` only chooses the tint.
function Robot({
  joints,
  onLoad,
  measured = false,
}: Pick<Props, "joints" | "onLoad"> & { measured?: boolean }) {
  return (
    <Fr3Robot
      joints={joints}
      onLoad={onLoad}
      decorate={measured ? tintMeasured : undefined}
    />
  );
}

function SceneContents(props: Props) {
  const { onLoad } = props;
  const [dragging, setDragging] = useState(false);
  const [armReady, setArmReady] = useState<boolean | null>(null);
  const [toolReady, setToolReady] = useState<boolean | null>(null);
  const armLoaded = useCallback((ok: boolean) => setArmReady(ok), []);
  const toolLoaded = useCallback((ok: boolean) => setToolReady(ok), []);
  useEffect(() => {
    if (armReady === false || toolReady === false) onLoad(false);
    else if (armReady && toolReady) onLoad(true);
  }, [armReady, toolReady, onLoad]);
  const targetObject = useMemo(() => new THREE.Group(), []);
  const { invalidate } = useThree();
  useEffect(() => {
    targetObject.position.set(...props.target.position);
    targetObject.quaternion.set(...props.target.quaternion);
    targetObject.updateMatrixWorld(true);
    invalidate();
  }, [targetObject, props.target, invalidate]);
  const { camera, gl, scene, controls: orbitControls } = useThree();
  const callbacks = useRef<TargetCallbacks>({
    onTarget: props.onTarget,
    onStart: props.onDragStart,
    onEnd: props.onDragEnd,
  });
  callbacks.current = {
    onTarget: props.onTarget,
    onStart: () => {
      if (orbitControls instanceof OrbitControlsImpl)
        orbitControls.enabled = false;
      setDragging(true);
      props.onDragStart();
    },
    onEnd: () => {
      if (orbitControls instanceof OrbitControlsImpl)
        orbitControls.enabled = true;
      setDragging(false);
      props.onDragEnd();
    },
  };
  useEffect(() => {
    if (props.mode === "view") return;
    return bindTargetControls({
      camera,
      element: gl.domElement,
      scene,
      target: targetObject,
      mode: props.mode,
      space: props.space,
      callbacks: () => callbacks.current,
      invalidate,
    });
  }, [
    camera,
    gl,
    scene,
    targetObject,
    props.mode,
    props.space,
    props.cancelRevision,
    invalidate,
  ]);
  return (
    <>
      <Fr3Backdrop />
      <group visible={props.poseVisible}>
        <Robot joints={props.joints} onLoad={armLoaded} />
        <group visible={props.toolVisible}>
          <ToolModel
            joints={props.joints}
            openingRatio={props.openingRatio}
            draft={props.toolDraft}
            onLoad={toolLoaded}
          />
        </group>
      </group>
      {props.measuredJoints && (
        <Robot joints={props.measuredJoints} onLoad={IGNORE_LOAD} measured />
      )}
      {props.measuredJoints && props.measuredOpeningRatio !== null && (
        <ToolModel
          joints={props.measuredJoints}
          openingRatio={props.measuredOpeningRatio}
          draft={props.toolDraft}
          onLoad={IGNORE_LOAD}
          measured
        />
      )}
      {props.workspaceBox && (
        <mesh
          position={
            props.workspaceBox.min.map(
              (v, i) => (v + props.workspaceBox!.max[i]) / 2,
            ) as [number, number, number]
          }
        >
          <boxGeometry
            args={
              props.workspaceBox.max.map(
                (v, i) => v - props.workspaceBox!.min[i],
              ) as [number, number, number]
            }
          />
          <meshBasicMaterial
            color={mediaColor(3)}
            wireframe
            transparent
            opacity={0.5}
            depthWrite={false}
          />
        </mesh>
      )}
      {props.waypoints.map((point, index) => (
        <mesh key={index} position={point.pose.position}>
          <sphereGeometry args={[0.008, 12, 8]} />
          <meshBasicMaterial color={mediaColor(2)} />
        </mesh>
      ))}
      <primitive object={targetObject} visible={props.poseVisible}>
        <axesHelper args={[0.11]} />
        <mesh>
          <sphereGeometry args={[0.009, 16, 10]} />
          <meshBasicMaterial color={mediaColor(3)} />
        </mesh>
      </primitive>
      <OrbitControls
        makeDefault
        enabled={!dragging}
        minDistance={0.35}
        maxDistance={4}
        target={[0.25, 0, 0.4]}
      />
      <Fr3CameraView view={props.view} revision={props.viewRevision} />
    </>
  );
}

export default function RobotStudioScene(props: Props) {
  return (
    <Canvas {...FR3_CANVAS_PROPS}>
      <SceneContents {...props} />
    </Canvas>
  );
}
