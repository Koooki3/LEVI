"use client";
// The dataset viewer's 3D replay: an FR3 arm (joints mode), or only the
// end-effector path and coordinate axes (pose mode), following the episode's
// video. Loaded lazily (`ssr: false`) by the Episodes panel.
//
// It only receives data. Time comes in through `subscribe` (the TimeProvider)
// and the primary video (`startReplayClock`); each update samples the packed
// rows, poses the model(s) through their handles and calls `invalidate()`
// (the Canvas draws on demand). Nothing on that path goes through React state,
// so 30 updates a second do not re-render anything.
import { useCallback, useEffect, useMemo, useRef } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import { usePrefersReducedMotion } from "@/lib/design/motion";
import type { ReplayData } from "@/lib/replay/frames";
import {
  startReplayClock,
  type ReplayClockOptions,
  type ReplayVideoSource,
} from "@/lib/replay/playback-clock";
import { createReplaySampler } from "@/lib/replay/sampler";
import {
  FR3_CANVAS_PROPS,
  FR3_GL_PARAMETERS,
  FR3_ORBIT_TARGET,
  Fr3Backdrop,
  Fr3CameraView,
  Fr3Robot,
  type Fr3RobotHandle,
  type Fr3View,
} from "./fr3-model";
import { createReplayRig } from "./replay-rig";
import { createGuardedRenderer } from "./webgl-guard";

export interface ReplayScenePropsBase {
  data: ReplayData;
  /** Draw the action as a translucent second arm (joints) or axes (pose). */
  showAction: boolean;
  /** Draw the last <= 300 end-effector positions. */
  trail: boolean;
  view: Fr3View;
  /** Bump to re-apply `view` (the view buttons do, so a second click resets the camera). */
  viewRevision: number;
  /** `useTime().subscribe`. */
  subscribe: ReplayClockOptions["subscribe"];
  /** The primary video; keep the object stable between renders. */
  video: ReplayVideoSource | null;
  /** Episode time to show until the clock reports one. */
  initialTime: number;
  /** false: the model or WebGL failed (the panel shows its retry state). */
  onLoad: (ok: boolean) => void;
}

function ReplayContents({
  data,
  showAction,
  trail,
  view,
  viewRevision,
  subscribe,
  video,
  initialTime,
  onLoad,
}: ReplayScenePropsBase) {
  const { invalidate } = useThree();
  // drei's OrbitControls keeps turning after a drag (damping) unless told not to.
  const reducedMotion = usePrefersReducedMotion();
  const sampler = useMemo(() => createReplaySampler(data), [data]);
  const rig = useMemo(() => createReplayRig(data, sampler), [data, sampler]);
  useEffect(() => () => rig.dispose(), [rig]);

  const solid = useRef<Fr3RobotHandle>(null);
  const ghost = useRef<Fr3RobotHandle>(null);
  const flags = useRef({ showAction, trail });
  flags.current = { showAction, trail };
  const lastTime = useRef(initialTime);
  const onLoadRef = useRef(onLoad);
  onLoadRef.current = onLoad;

  const apply = useCallback(
    (t: number) => {
      lastTime.current = t;
      const frame = sampler.at(t);
      if (data.mode === "joints") solid.current?.setJoints(frame.joints);
      if (flags.current.showAction && data.actionMode === "joints") {
        ghost.current?.setJoints(frame.actionJoints);
      }
      rig.update(frame, flags.current);
      invalidate();
    },
    [data, sampler, rig, invalidate],
  );

  // Show the current time now, and again whenever a toggle changes what is drawn.
  useEffect(() => apply(lastTime.current), [apply, showAction, trail]);
  useEffect(
    () => startReplayClock({ subscribe, video, onTime: apply }),
    [subscribe, video, apply],
  );

  // A model that finishes loading late is posed at the time then, not at the
  // time it was asked for. The callbacks are stable (through a ref): a new
  // `onLoad` would make the model load again.
  const applyRef = useRef(apply);
  applyRef.current = apply;
  const solidLoaded = useCallback((ok: boolean) => {
    if (ok) applyRef.current(lastTime.current);
    onLoadRef.current(ok);
  }, []);
  const ghostLoaded = useCallback((ok: boolean) => {
    if (ok) applyRef.current(lastTime.current);
  }, []);
  // Pose mode has no model to wait for.
  useEffect(() => {
    if (data.mode === "pose") onLoadRef.current(true);
  }, [data.mode]);

  return (
    <>
      <Fr3Backdrop />
      {data.mode === "joints" && (
        <Fr3Robot
          ref={solid}
          joints={sampler.frame.joints}
          onLoad={solidLoaded}
        />
      )}
      {data.mode === "joints" && data.actionMode === "joints" && showAction && (
        <Fr3Robot
          ref={ghost}
          ghost
          joints={sampler.frame.actionJoints}
          onLoad={ghostLoaded}
        />
      )}
      <primitive object={rig.group} />
      <OrbitControls
        makeDefault
        enableDamping={!reducedMotion}
        minDistance={0.35}
        maxDistance={4}
        target={FR3_ORBIT_TARGET}
      />
      <Fr3CameraView view={view} revision={viewRevision} />
    </>
  );
}

export default function ReplayScene(props: ReplayScenePropsBase) {
  // No WebGL: the renderer cannot be made, which <Canvas> does not report by
  // itself (see webgl-guard.ts). One guard per mount; Retry mounts a new scene.
  const onLoadRef = useRef(props.onLoad);
  onLoadRef.current = props.onLoad;
  const gl = useMemo(
    () =>
      createGuardedRenderer(FR3_GL_PARAMETERS, (error) => {
        console.warn("3D replay: could not create a WebGL context.", error);
        onLoadRef.current(false);
      }),
    [],
  );
  return (
    <Canvas {...FR3_CANVAS_PROPS} gl={gl}>
      <ReplayContents {...props} />
    </Canvas>
  );
}
