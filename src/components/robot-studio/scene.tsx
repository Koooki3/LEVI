"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import URDFLoader, { type URDFRobot } from "urdf-loader";
import { robotGlbLoader } from "@/lib/robot-studio/glb-loader";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { bindTargetControls, type TargetCallbacks } from "./transform-session";
import {
  MEDIA_BACKGROUND,
  MEDIA_GRID,
  MEDIA_GRID_SECTION,
  URDF_LIGHT,
  URDF_MATERIAL,
  mediaColor,
} from "@/components/viewer/data-palette";
import { FR3_JOINT_NAMES } from "@/lib/robot-studio/fr3-kinematics";
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

function disposeObject(object: THREE.Object3D) {
  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    child.geometry.dispose();
    const materials = Array.isArray(child.material)
      ? child.material
      : [child.material];
    for (const material of materials) material.dispose();
  });
}

function Robot({
  joints,
  onLoad,
  measured = false,
}: Pick<Props, "joints" | "onLoad"> & { measured?: boolean }) {
  const [robot, setRobot] = useState<URDFRobot | null>(null);
  const { invalidate } = useThree();
  const currentJoints = useRef(joints);
  currentJoints.current = joints;
  useEffect(() => {
    let cancelled = false;
    let failed = false;
    let loaded: URDFRobot | null = null;
    const manager = new THREE.LoadingManager();
    manager.onError = () => {
      failed = true;
      if (!cancelled) onLoad(false);
    };
    const loader = new URDFLoader(manager);
    // The typed error callback requires an Object3D; URDFLoader ignores it when err is set.
    loader.loadMeshCb = (path, resourceManager, done) => {
      if (path.endsWith(".glb")) {
        robotGlbLoader(resourceManager).load(
          path,
          (gltf) => done(gltf.scene),
          undefined,
          (error) => done(new THREE.Group(), error as Error),
        );
      } else {
        new STLLoader(resourceManager).load(
          path,
          (geometry) => {
            const group = new THREE.Group();
            group.add(
              new THREE.Mesh(
                geometry,
                new THREE.MeshStandardMaterial({
                  color: URDF_MATERIAL.neutral,
                  roughness: 0.6,
                }),
              ),
            );
            done(group);
          },
          undefined,
          (error) => done(new THREE.Group(), error as Error),
        );
      }
    };
    // LoadingManager completion may precede the final loader callback.
    manager.onLoad = () =>
      queueMicrotask(() => {
        if (cancelled) {
          if (loaded) disposeObject(loaded);
          return;
        }
        if (!loaded || failed) return;
        FR3_JOINT_NAMES.forEach((name, i) =>
          loaded?.setJointValue(name, currentJoints.current[i]),
        );
        if (measured) tintMeasured(loaded);
        setRobot(loaded);
        onLoad(true);
        invalidate();
      });
    loader.load(
      "/robots/fr3/fr3.urdf",
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
      if (loaded) disposeObject(loaded);
    };
  }, [invalidate, onLoad, measured]);
  useEffect(() => {
    if (!robot) return;
    FR3_JOINT_NAMES.forEach((name, i) => robot.setJointValue(name, joints[i]));
    robot.updateMatrixWorld(true);
    invalidate();
  }, [robot, joints, invalidate]);
  return robot ? <primitive object={robot} /> : null;
}

function CameraView({
  view,
  revision,
}: {
  view: Props["view"];
  revision: number;
}) {
  const { camera, controls, invalidate } = useThree();
  useEffect(() => {
    const positions = {
      perspective: [1.35, 1.35, 1.05],
      front: [1.6, 0, 0.55],
      top: [0.35, 0, 1.8],
    } as const;
    camera.up.set(0, 0, 1);
    const [x, y, z] = positions[view];
    camera.position.set(x, y, z);
    if (controls instanceof OrbitControlsImpl) {
      controls.target.set(0.25, 0, 0.4);
      controls.update();
    }
    camera.lookAt(0.25, 0, 0.4);
    invalidate();
  }, [camera, controls, invalidate, revision, view]);
  return null;
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
      <color attach="background" args={[MEDIA_BACKGROUND]} />
      <ambientLight intensity={0.8} />
      <directionalLight
        color={URDF_LIGHT.key}
        position={[2, 1, 3]}
        intensity={2.3}
      />
      <directionalLight
        color={URDF_LIGHT.fill}
        position={[-2, 1, 1]}
        intensity={1.2}
      />
      <directionalLight
        color={URDF_LIGHT.rim}
        position={[0, -2, 2]}
        intensity={1.8}
      />
      <gridHelper
        args={[2, 20, MEDIA_GRID_SECTION, MEDIA_GRID]}
        rotation={[Math.PI / 2, 0, 0]}
      />
      <axesHelper args={[0.16]} />
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
      <CameraView view={props.view} revision={props.viewRevision} />
    </>
  );
}

export default function RobotStudioScene(props: Props) {
  return (
    <Canvas
      frameloop="demand"
      dpr={[1, 1.5]}
      camera={{
        position: [1.35, 1.35, 1.05],
        up: [0, 0, 1],
        fov: 40,
        near: 0.01,
        far: 20,
      }}
      gl={{ antialias: true, toneMapping: THREE.ACESFilmicToneMapping }}
    >
      <SceneContents {...props} />
    </Canvas>
  );
}
