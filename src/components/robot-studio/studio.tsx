"use client";
import dynamic from "next/dynamic";
import Link from "next/link";
import {
  Component,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Download,
  Expand,
  Move3D,
  Plus,
  Rotate3D,
  RotateCcw,
} from "lucide-react";
import { Badge, Button, Select } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  FR3_INITIAL_JOINTS,
  FR3_JOINT_NAMES,
  FR3_JOINT_LIMITS,
  forwardPose,
  solvePose,
} from "@/lib/robot-studio/fr3-kinematics";
import {
  MAX_WAYPOINTS,
  exportPreview,
  parsePoseFields,
  poseFields,
  previewWaypoint,
  type PoseFields,
  type PreviewPose,
  type Waypoint,
} from "./preview-state";
import type { StudioMode } from "./scene";
import type { RobotStudioMeasurement } from "@/types/robotStudio";
import ToolPanel from "./tool-panel";
import {
  DEFAULT_TOOL_DRAFT,
  isValidPreviewPose,
  EMPTY_TELEOP_DRAFT,
  type ToolDraft,
  type TeleopDraft,
} from "./tool-draft";
import TelemetryPanel from "./telemetry-panel";
import { measuredOpeningRatio } from "./telemetry-state";
import WorkspacePanel from "./workspace-panel";
import ManualControls from "./manual-controls";
import ParameterPanel from "./parameter-panel";
import type { ManualAnchor } from "./manual-api";
import type { ManualDragController } from "./manual-session";
import { pointInWorkspace, type WorkspaceBox } from "./workspace-draft";

const Scene = dynamic(() => import("./scene"), { ssr: false });
const FIELD_NAMES = ["X", "Y", "Z", "RX", "RY", "RZ"] as const;
const START = forwardPose([...FR3_INITIAL_JOINTS]);
type SolveState = {
  kind: "ready" | "unreachable" | "invalid" | "outside";
  positionError: number;
  orientationError: number;
};

class SceneBoundary extends Component<
  { children: ReactNode; onFailure: () => void },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch() {
    this.props.onFailure();
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}

export default function RobotStudio() {
  const { t } = useLocale();
  const [openingRatio, setOpeningRatio] = useState(1);
  const [toolDraft, setToolDraft] = useState<ToolDraft>(DEFAULT_TOOL_DRAFT);
  const [teleopDraft, setTeleopDraft] =
    useState<TeleopDraft>(EMPTY_TELEOP_DRAFT);
  const [workspaceBox, setWorkspaceBox] = useState<WorkspaceBox | null>(null);
  const [measurement, setMeasurement] = useState<RobotStudioMeasurement | null>(
    null,
  );
  const [syncMode, setSyncMode] = useState<"offline" | "follow" | "edit">(
    "offline",
  );
  const syncModeRef = useRef(syncMode);
  const latestMeasuredRef = useRef<RobotStudioMeasurement | null>(null);
  const [lastFollowSample, setLastFollowSample] =
    useState<RobotStudioMeasurement | null>(null);
  const [joints, setJoints] = useState<number[]>([...FR3_INITIAL_JOINTS]);
  const jointsRef = useRef(joints);
  const [target, setTarget] = useState<PreviewPose>(START);
  const [fields, setFields] = useState<PoseFields>(() => poseFields(START));
  const [solve, setSolve] = useState<SolveState>({
    kind: "ready",
    positionError: 0,
    orientationError: 0,
  });
  const [mode, setMode] = useState<StudioMode>("translate");
  const [space, setSpace] = useState<"world" | "local">("world");
  const [view, setView] = useState<"perspective" | "front" | "top">(
    "perspective",
  );
  const [viewRevision, setViewRevision] = useState(0);
  const [cancelRevision, setCancelRevision] = useState(0);
  const [model, setModel] = useState<"loading" | "ready" | "failed">("loading");
  const [sceneKey, setSceneKey] = useState(0);
  const [waypoints, setWaypoints] = useState<Waypoint[]>([]);
  const [dragging, setDragging] = useState(false);
  const dragStart = useRef<{
    joints: number[];
    target: PreviewPose;
    solve: SolveState;
  } | null>(null);
  const frame = useRef<number | null>(null);
  const pendingTarget = useRef<PreviewPose | null>(null);
  const manualController = useRef<ManualDragController | null>(null);
  const receiveManualController = useCallback(
    (controller: ManualDragController | null) => {
      manualController.current = controller;
    },
    [],
  );

  const updateJoints = useCallback((next: number[]) => {
    jointsRef.current = next;
    setJoints(next);
  }, []);
  const solveTarget = useCallback(
    (next: PreviewPose, box = workspaceBox) => {
      try {
        if (!isValidPreviewPose(next)) throw new RangeError("Invalid target");
        setTarget(next);
        setFields(poseFields(next));
        if (box && !pointInWorkspace(box, next.position)) {
          setSolve({ kind: "outside", positionError: 0, orientationError: 0 });
          return false;
        }
        const result = solvePose(next, jointsRef.current);
        setSolve({
          kind: result.converged ? "ready" : "unreachable",
          positionError: result.positionError,
          orientationError: result.orientationError,
        });
        if (result.converged) updateJoints([...result.joints]);
        return result.converged;
      } catch {
        setSolve({ kind: "invalid", positionError: 0, orientationError: 0 });
        return false;
      }
    },
    [updateJoints, workspaceBox],
  );
  const cancelPending = useCallback(() => {
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = null;
    pendingTarget.current = null;
  }, []);
  useEffect(() => cancelPending, [cancelPending]);
  const receiveManualAnchor = useCallback(
    (anchor: ManualAnchor | null) => {
      if (!anchor) return;
      cancelPending();
      const next = [...anchor.joints_rad];
      updateJoints(next);
      solveTarget(forwardPose(next));
    },
    [cancelPending, updateJoints, solveTarget],
  );
  const onTarget = useCallback(
    (next: PreviewPose) => {
      if (syncModeRef.current === "follow") return;
      pendingTarget.current = next;
      if (frame.current !== null) return;
      frame.current = requestAnimationFrame(() => {
        frame.current = null;
        const latest = pendingTarget.current;
        pendingTarget.current = null;
        if (latest) {
          if (solveTarget(latest))
            manualController.current?.target(latest.position);
          else manualController.current?.revoke("preview_invalid");
        }
      });
    },
    [solveTarget],
  );
  const onDragStart = useCallback(() => {
    if (syncModeRef.current === "follow") return;
    dragStart.current = { joints: [...jointsRef.current], target, solve };
    manualController.current?.beginDrag();
    setDragging(true);
  }, [target, solve]);
  const onDragEnd = useCallback(() => {
    cancelPending();
    manualController.current?.revoke("drag_end");
    setDragging(false);
  }, [cancelPending]);
  const cancelDrag = useCallback(() => {
    manualController.current?.revoke("drag_cancel");
    cancelPending();
    const previous = dragStart.current;
    if (!previous) return;
    updateJoints(previous.joints);
    setTarget(previous.target);
    setFields(poseFields(previous.target));
    setSolve(previous.solve);
    setDragging(false);
    setCancelRevision((v) => v + 1);
    dragStart.current = null;
  }, [cancelPending, updateJoints]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && dragging) {
        event.preventDefault();
        cancelDrag();
      }
    };
    const onBlur = () => {
      if (dragging) cancelDrag();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onBlur);
    };
  }, [dragging, cancelDrag]);
  const onLoad = useCallback(
    (ok: boolean) => setModel(ok ? "ready" : "failed"),
    [],
  );
  const onSceneFailure = useCallback(() => setModel("failed"), []);
  const applyFollowMeasurement = useCallback(
    (measured: RobotStudioMeasurement) => {
      // Feedback is displayed as received, even outside the draft workspace.
      // Bounds govern editing, never alter or clamp the measured robot pose.
      cancelPending();
      const next = [...measured.joints_rad];
      const pose = forwardPose(next);
      updateJoints(next);
      setTarget(pose);
      setFields(poseFields(pose));
      setLastFollowSample(measured);
      const ratio = measuredOpeningRatio(measured);
      if (ratio !== null) setOpeningRatio(ratio);
      setSolve({
        kind:
          workspaceBox && !pointInWorkspace(workspaceBox, pose.position)
            ? "outside"
            : "ready",
        positionError: 0,
        orientationError: 0,
      });
    },
    [cancelPending, updateJoints, workspaceBox],
  );
  const receiveMeasurement = useCallback(
    (measured: RobotStudioMeasurement | null) => {
      setMeasurement(measured);
      if (!measured) return;
      latestMeasuredRef.current = measured;
      if (syncModeRef.current === "follow") applyFollowMeasurement(measured);
    },
    [applyFollowMeasurement],
  );
  const changeSyncMode = (next: typeof syncMode) => {
    cancelPending();
    setDragging(false);
    dragStart.current = null;
    setCancelRevision((v) => v + 1);
    syncModeRef.current = next;
    setSyncMode(next);
  };
  const changeReading = (enabled: boolean) => {
    changeSyncMode(enabled ? "follow" : "offline");
    setMeasurement(null);
    if (enabled) {
      latestMeasuredRef.current = null;
      setLastFollowSample(null);
    }
  };
  const followMeasurement = (measured: RobotStudioMeasurement | null) => {
    changeSyncMode("follow");
    // A fresh click aligns immediately. Otherwise keep the latest measured
    // pose visibly stale, including feedback received while editing the target.
    if (measured) receiveMeasurement(measured);
    else {
      setMeasurement(null);
      const last = latestMeasuredRef.current;
      if (last) applyFollowMeasurement(last);
      else setLastFollowSample(null);
    }
  };
  const acceptJointPreview = (next: number[], ratio?: number | null) => {
    cancelPending();
    const pose = forwardPose(next);
    setTarget(pose);
    setFields(poseFields(pose));
    if (workspaceBox && !pointInWorkspace(workspaceBox, pose.position)) {
      setSolve({ kind: "outside", positionError: 0, orientationError: 0 });
      return false;
    }
    updateJoints(next);
    if (ratio !== undefined && ratio !== null) setOpeningRatio(ratio);
    setSolve({ kind: "ready", positionError: 0, orientationError: 0 });
    return true;
  };
  const reset = () => {
    if (syncModeRef.current === "follow") return;
    acceptJointPreview([...FR3_INITIAL_JOINTS], 1);
    setDragging(false);
    dragStart.current = null;
    setCancelRevision((v) => v + 1);
  };
  const applyFields = () => {
    if (syncModeRef.current === "follow") return;
    cancelPending();
    const next = parsePoseFields(fields);
    if (!next) {
      setSolve({ kind: "invalid", positionError: 0, orientationError: 0 });
      return;
    }
    solveTarget(next);
  };
  const changeJoint = (index: number, value: number) => {
    if (syncModeRef.current === "follow") return;
    const next = [...jointsRef.current];
    next[index] = value;
    acceptJointPreview(next);
  };
  const useMeasuredPreview = (measured: RobotStudioMeasurement) => {
    const accepted = acceptJointPreview(
      [...measured.joints_rad],
      measuredOpeningRatio(measured),
    );
    if (!accepted) return;
    if (syncModeRef.current !== "offline") changeSyncMode("edit");
    else {
      setDragging(false);
      dragStart.current = null;
      setCancelRevision((v) => v + 1);
    }
  };
  const download = () => {
    const blob = new Blob(
      [exportPreview(waypoints, { teleopDraft, workspaceBox })],
      {
        type: "application/json",
      },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "fr3-preview.json";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const dirtyFields = fields.some((v, i) => v !== poseFields(target)[i]);
  const ready = model === "ready";
  const following = syncMode === "follow";
  const editable = ready && !following;
  const activeMode = editable ? mode : "view";
  const waitingForPose = following && !lastFollowSample;
  const followStatus = measurement
    ? "following"
    : lastFollowSample
      ? "lostFeedback"
      : "waitingForPose";
  const canSave =
    editable &&
    solve.kind === "ready" &&
    (!workspaceBox ||
      pointInWorkspace(workspaceBox, target.position) === true) &&
    !dirtyFields &&
    !dragging &&
    waypoints.length < MAX_WAYPOINTS;

  return (
    <>
      <header className="rs-header">
        <div>
          <p className="rs-eyebrow">{t("robotStudio.eyebrow")}</p>
          <h1>{t("robotStudio.title")}</h1>
          <p className="rs-description">{t("robotStudio.description")}</p>
        </div>
        <div className="rs-header-links">
          <Badge>
            {t(
              syncMode === "offline"
                ? "robotStudio.offline"
                : "robotStudio.state.readOnlyMapping",
            )}
          </Badge>
          <Link className="ds-focus rs-link" href="/marvel_workstation">
            {t("robotStudio.console")}
          </Link>
        </div>
      </header>
      <div className="rs-layout">
        <section className="rs-stage" aria-label={t("robotStudio.viewport")}>
          <div className="rs-toolbar">
            <div
              className="rs-modes"
              role="group"
              aria-label={t("robotStudio.mode")}
            >
              <Button
                size="sm"
                variant={activeMode === "view" ? "primary" : "ghost"}
                aria-pressed={activeMode === "view"}
                onClick={() => setMode("view")}
              >
                {t("robotStudio.view")}
              </Button>
              <Button
                size="sm"
                icon={Move3D}
                disabled={following}
                variant={activeMode === "translate" ? "primary" : "ghost"}
                aria-pressed={activeMode === "translate"}
                onClick={() => setMode("translate")}
              >
                {t("robotStudio.translate")}
              </Button>
              <Button
                size="sm"
                icon={Rotate3D}
                disabled={following}
                variant={activeMode === "rotate" ? "primary" : "ghost"}
                aria-pressed={activeMode === "rotate"}
                onClick={() => setMode("rotate")}
              >
                {t("robotStudio.rotate")}
              </Button>
            </div>
            <label className="rs-inline-field">
              <span>{t("robotStudio.axes")}</span>
              <Select
                aria-label={t("robotStudio.axes")}
                value={space}
                onChange={(e) => setSpace(e.target.value as "world" | "local")}
              >
                <option value="world">{t("robotStudio.base")}</option>
                <option value="local">{t("robotStudio.flange")}</option>
              </Select>
            </label>
          </div>
          <div className="rs-canvas" data-theme="dark">
            <SceneBoundary key={sceneKey} onFailure={onSceneFailure}>
              <Scene
                joints={joints}
                poseVisible={!waitingForPose}
                toolVisible={
                  !following || measuredOpeningRatio(lastFollowSample) !== null
                }
                openingRatio={openingRatio}
                toolDraft={toolDraft}
                measuredJoints={
                  !following && measurement ? [...measurement.joints_rad] : null
                }
                measuredOpeningRatio={measuredOpeningRatio(measurement)}
                workspaceBox={workspaceBox}
                target={target}
                mode={activeMode}
                space={space}
                view={view}
                viewRevision={viewRevision}
                cancelRevision={cancelRevision}
                waypoints={waypoints}
                onTarget={onTarget}
                onDragStart={onDragStart}
                onDragEnd={onDragEnd}
                onLoad={onLoad}
              />
            </SceneBoundary>
            {model !== "ready" && (
              <div
                className="rs-overlay"
                role={model === "failed" ? "alert" : "status"}
              >
                <p>
                  {model === "failed"
                    ? t("robotStudio.loadError")
                    : t("robotStudio.loading")}
                </p>
                {model === "failed" && (
                  <Button
                    onClick={() => {
                      setModel("loading");
                      setSceneKey((v) => v + 1);
                    }}
                  >
                    {t("robotStudio.retry")}
                  </Button>
                )}
              </div>
            )}
            <div className="rs-scene-caption">
              <span>
                {t(
                  following
                    ? "robotStudio.state." + followStatus
                    : "robotStudio.modelLabel",
                )}
              </span>
              <span>
                {t(
                  following
                    ? "robotStudio.state.measuredFlange"
                    : "robotStudio.targetLabel",
                )}
              </span>
            </div>
          </div>
          <div className="rs-viewbar">
            <label className="rs-inline-field">
              <span>{t("robotStudio.camera")}</span>
              <Select
                aria-label={t("robotStudio.camera")}
                value={view}
                onChange={(e) => setView(e.target.value as typeof view)}
              >
                <option value="perspective">
                  {t("robotStudio.perspective")}
                </option>
                <option value="front">{t("robotStudio.front")}</option>
                <option value="top">{t("robotStudio.top")}</option>
              </Select>
            </label>
            <Button
              size="sm"
              variant="ghost"
              icon={Expand}
              onClick={() => setViewRevision((v) => v + 1)}
            >
              {t("robotStudio.fit")}
            </Button>
          </div>
          <p className="rs-hint">
            {t(
              following
                ? "robotStudio.state.followHint"
                : "robotStudio.dragHint",
            )}
          </p>
        </section>
        <aside className="rs-inspector" aria-label={t("robotStudio.target")}>
          <section className="rs-panel-section">
            <h2>
              {t(
                following
                  ? "robotStudio.state.measuredPose"
                  : "robotStudio.target",
              )}
            </h2>
            <p className="rs-meta">{t("robotStudio.frameHint")}</p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                applyFields();
              }}
            >
              <fieldset disabled={!editable}>
                <legend>{t("robotStudio.position")}</legend>
                <div className="rs-fields">
                  {FIELD_NAMES.slice(0, 3).map((name, i) => (
                    <label key={name}>
                      <span>
                        {name}
                        <small>mm</small>
                      </span>
                      <input
                        className="ds-input ds-focus"
                        aria-label={`${name} (mm)`}
                        type="number"
                        step="0.1"
                        min="-2000"
                        max="2000"
                        value={waitingForPose ? "" : fields[i]}
                        onChange={(e) =>
                          setFields(
                            (old) =>
                              old.map((v, n) =>
                                n === i ? e.target.value : v,
                              ) as PoseFields,
                          )
                        }
                      />
                    </label>
                  ))}
                </div>
              </fieldset>
              <fieldset disabled={!editable}>
                <legend>{t("robotStudio.orientation")}</legend>
                <div className="rs-fields">
                  {FIELD_NAMES.slice(3).map((name, i) => (
                    <label key={name}>
                      <span>
                        {name}
                        <small>°</small>
                      </span>
                      <input
                        className="ds-input ds-focus"
                        aria-label={`${name} (°)`}
                        type="number"
                        step="0.1"
                        min="-360"
                        max="360"
                        value={waitingForPose ? "" : fields[i + 3]}
                        onChange={(e) =>
                          setFields(
                            (old) =>
                              old.map((v, n) =>
                                n === i + 3 ? e.target.value : v,
                              ) as PoseFields,
                          )
                        }
                      />
                    </label>
                  ))}
                </div>
              </fieldset>
              <Button type="submit" disabled={!editable}>
                {t("robotStudio.apply")}
              </Button>
            </form>
            <div
              className="rs-solver"
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              <strong>
                {following
                  ? t("robotStudio.state." + followStatus)
                  : solve.kind === "ready"
                    ? t("robotStudio.solved")
                    : solve.kind === "outside"
                      ? t("robotStudio.workspace.outside")
                      : solve.kind === "invalid"
                        ? t("robotStudio.invalid")
                        : t("robotStudio.unreachable")}
              </strong>
              {!following && solve.kind !== "ready" && (
                <p>{t("robotStudio.lastValid")}</p>
              )}
              {following && !waitingForPose && solve.kind === "outside" && (
                <p>{t("robotStudio.state.measuredOutside")}</p>
              )}
            </div>
            {solve.kind === "unreachable" && (
              <p className="rs-meta">
                {t("robotStudio.error")}{" "}
                {Number.isFinite(solve.positionError)
                  ? (solve.positionError * 1000).toFixed(1)
                  : "—"}{" "}
                mm ·{" "}
                {Number.isFinite(solve.orientationError)
                  ? ((solve.orientationError * 180) / Math.PI).toFixed(1)
                  : "—"}
                °
              </p>
            )}
            <Button
              icon={RotateCcw}
              variant="ghost"
              size="sm"
              disabled={!editable}
              onClick={reset}
            >
              {t("robotStudio.reset")}
            </Button>
          </section>
          <section className="rs-panel-section">
            <details>
              <summary>{t("robotStudio.joints")}</summary>
              <div className="rs-joints">
                {FR3_JOINT_NAMES.map((name, i) => (
                  <label key={name}>
                    <span>
                      {name.replace("fr3_joint", "J")}
                      <output>
                        {waitingForPose
                          ? "—"
                          : ((joints[i] * 180) / Math.PI).toFixed(1) + "°"}
                      </output>
                    </span>
                    <input
                      type="range"
                      aria-label={name}
                      min={FR3_JOINT_LIMITS[i].lower}
                      max={FR3_JOINT_LIMITS[i].upper}
                      step="0.001"
                      value={joints[i]}
                      disabled={!editable}
                      hidden={waitingForPose}
                      onChange={(e) => changeJoint(i, Number(e.target.value))}
                    />
                  </label>
                ))}
              </div>
            </details>
          </section>
          <div hidden={following}>
            <ToolPanel
              openingRatio={openingRatio}
              onOpeningRatio={setOpeningRatio}
              draft={toolDraft}
              onDraft={setToolDraft}
              teleopDraft={teleopDraft}
              onTeleopDraft={setTeleopDraft}
              disabled={!editable || dragging}
            />
          </div>
          <WorkspacePanel
            value={workspaceBox}
            targetPosition={waitingForPose ? null : target.position}
            onChange={(box) => {
              cancelPending();
              setWorkspaceBox(box);
              if (following) {
                setSolve({
                  kind:
                    box && !pointInWorkspace(box, target.position)
                      ? "outside"
                      : "ready",
                  positionError: 0,
                  orientationError: 0,
                });
              } else solveTarget(target, box);
            }}
            disabled={!ready || dragging}
          />
          <TelemetryPanel
            onMeasurement={receiveMeasurement}
            onEnabledChange={changeReading}
            following={following}
            onFollowMeasurement={followMeasurement}
            onUseMeasurement={useMeasuredPreview}
            canUsePreview={ready && !dragging}
          />
          <ParameterPanel />
          <ManualControls
            workspaceBox={workspaceBox}
            teleopDraft={teleopDraft}
            targetPosition={target.position}
            canDrag={editable && mode === "translate" && solve.kind === "ready"}
            onSessionAnchor={receiveManualAnchor}
            onController={receiveManualController}
          />
          <section className="rs-panel-section">
            <h2>{t("robotStudio.waypoints")}</h2>
            <p className="rs-meta">{t("robotStudio.waypointHint")}</p>
            <div className="rs-waypoint-count">
              <strong>{waypoints.length}</strong>
              <span>/ {MAX_WAYPOINTS}</span>
            </div>
            <div className="rs-actions">
              <Button
                icon={Plus}
                size="sm"
                disabled={!canSave}
                onClick={() =>
                  setWaypoints((old) => [
                    ...old,
                    previewWaypoint(joints, { openingRatio, draft: toolDraft }),
                  ])
                }
              >
                {t("robotStudio.add")}
              </Button>
              <Button
                icon={Download}
                size="sm"
                disabled={!waypoints.length}
                onClick={download}
              >
                {t("robotStudio.export")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={!waypoints.length}
                onClick={() => setWaypoints((old) => old.slice(0, -1))}
              >
                {t("robotStudio.undo")}
              </Button>
            </div>
            {dirtyFields && (
              <p className="rs-meta">{t("robotStudio.applyFirst")}</p>
            )}
          </section>
        </aside>
      </div>
      <section className="rs-footnote">
        <h2>{t("robotStudio.scopeTitle")}</h2>
        <p>{t("robotStudio.scope")}</p>
        <details>
          <summary>{t("robotStudio.nextTitle")}</summary>
          <p>{t("robotStudio.next")}</p>
          <a className="rs-link ds-focus" href="/robots/fr3/SOURCE.md">
            {t("robotStudio.modelSource")}
          </a>
        </details>
      </section>
    </>
  );
}
