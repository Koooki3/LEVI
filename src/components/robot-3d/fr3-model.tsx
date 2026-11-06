"use client";
// The FR3 arm model, shared by the robot studio (`robot-studio/scene.tsx`) and
// the dataset viewer's 3D replay (`replay-scene.tsx`): URDF + GLB loading,
// disposal, lights, floor grid, camera presets and the Canvas settings.
// Moved out of scene.tsx without changing what it draws; the only additions
// are `decorate` / `ghost` (a tint applied once the model has loaded) and the
// imperative `setJoints` handle (a replay moves the arm 30 times a second and
// must not go through React renders).
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { useThree, type CanvasProps } from "@react-three/fiber";
import * as THREE from "three";
import { OrbitControls as OrbitControlsImpl } from "three-stdlib";
import URDFLoader, { type URDFRobot } from "urdf-loader";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { robotGlbLoader } from "@/lib/robot-studio/glb-loader";
import { FR3_JOINT_NAMES } from "@/lib/robot-studio/fr3-kinematics";
import {
  MEDIA_BACKGROUND,
  MEDIA_GRID,
  MEDIA_GRID_SECTION,
  URDF_LIGHT,
  URDF_MATERIAL,
  mediaColor,
} from "@/components/viewer/data-palette";

export type Fr3View = "perspective" | "front" | "top";

/** Where the orbit camera looks, in `fr3_link0` metres. */
export const FR3_ORBIT_TARGET: readonly [number, number, number] = [
  0.25, 0, 0.4,
];

/** The studio's Canvas settings, spread into `<Canvas>` by both scenes. */
export const FR3_CANVAS_PROPS: Pick<
  CanvasProps,
  "frameloop" | "dpr" | "camera" | "gl"
> = {
  frameloop: "demand",
  dpr: [1, 1.5],
  camera: {
    position: [1.35, 1.35, 1.05],
    up: [0, 0, 1],
    fov: 40,
    near: 0.01,
    far: 20,
  },
  gl: { antialias: true, toneMapping: THREE.ACESFilmicToneMapping },
};

export function disposeObject(object: THREE.Object3D) {
  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    child.geometry.dispose();
    const materials = Array.isArray(child.material)
      ? child.material
      : [child.material];
    for (const material of materials) material.dispose();
  });
}

/**
 * A translucent, tinted copy for the replay's action ghost. Its own tint, not
 * the studio's "measured" one: a ghost is a commanded target, never a
 * measurement. Mutates the (privately loaded) materials once, after loading.
 */
export function tintGhost(object: THREE.Object3D) {
  object.traverse((child) => {
    if (!(child instanceof THREE.Mesh)) return;
    for (const material of Array.isArray(child.material)
      ? child.material
      : [child.material]) {
      if ("color" in material && material.color instanceof THREE.Color)
        material.color.set(mediaColor(1));
      material.transparent = true;
      material.opacity = 0.35;
      material.depthWrite = false;
    }
  });
}

export type Fr3RobotHandle = {
  /**
   * Pose the arm now and request one redraw. A no-op until the model has
   * loaded; the `joints` prop is what a load applies, so a caller that keeps
   * the same (mutated) array in both places never shows a stale pose.
   */
  setJoints: (joints: ArrayLike<number>) => void;
};

type Fr3RobotProps = {
  joints: ArrayLike<number>;
  onLoad: (ok: boolean) => void;
  /** Run once on the loaded model before it is shown (a tint). */
  decorate?: (model: URDFRobot) => void;
  /** Shorthand for `decorate={tintGhost}`. */
  ghost?: boolean;
};

export const Fr3Robot = forwardRef<Fr3RobotHandle, Fr3RobotProps>(
  function Fr3Robot({ joints, onLoad, decorate, ghost = false }, ref) {
    const [robot, setRobot] = useState<URDFRobot | null>(null);
    const { invalidate } = useThree();
    const currentJoints = useRef(joints);
    currentJoints.current = joints;
    const loadedRobot = useRef<URDFRobot | null>(null);
    const tint = decorate ?? (ghost ? tintGhost : undefined);
    useImperativeHandle(
      ref,
      () => ({
        setJoints: (values) => {
          const model = loadedRobot.current;
          if (!model) return;
          FR3_JOINT_NAMES.forEach((name, i) =>
            model.setJointValue(name, values[i]),
          );
          model.updateMatrixWorld(true);
          invalidate();
        },
      }),
      [invalidate],
    );
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
          if (tint) tint(loaded);
          loadedRobot.current = loaded;
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
        loadedRobot.current = null;
        if (loaded) disposeObject(loaded);
      };
    }, [invalidate, onLoad, tint]);
    useEffect(() => {
      if (!robot) return;
      FR3_JOINT_NAMES.forEach((name, i) =>
        robot.setJointValue(name, joints[i]),
      );
      robot.updateMatrixWorld(true);
      invalidate();
    }, [robot, joints, invalidate]);
    return robot ? <primitive object={robot} /> : null;
  },
);

const VIEW_POSITIONS = {
  perspective: [1.35, 1.35, 1.05],
  front: [1.6, 0, 0.55],
  top: [0.35, 0, 1.8],
} as const;

export function Fr3CameraView({
  view,
  revision,
}: {
  view: Fr3View;
  revision: number;
}) {
  const { camera, controls, invalidate } = useThree();
  useEffect(() => {
    camera.up.set(0, 0, 1);
    const [x, y, z] = VIEW_POSITIONS[view];
    camera.position.set(x, y, z);
    const [tx, ty, tz] = FR3_ORBIT_TARGET;
    if (controls instanceof OrbitControlsImpl) {
      controls.target.set(tx, ty, tz);
      controls.update();
    }
    camera.lookAt(tx, ty, tz);
    invalidate();
  }, [camera, controls, invalidate, revision, view]);
  return null;
}

/** Black backdrop, the studio's three-point lights, floor grid and base axes. */
export function Fr3Backdrop() {
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
    </>
  );
}
