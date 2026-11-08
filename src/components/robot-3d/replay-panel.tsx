"use client";
// The 3D replay panel of the Episodes tab: beside the video(s) on a wide
// screen, below them on a narrow one. Joints mode draws the FR3; pose mode
// (no joint columns) draws only the end-effector path and axes, and says the
// pose frame is not verified. The scene itself (three.js, React Three Fiber)
// is loaded on demand and only when the panel is open.
import {
  Component,
  useCallback,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import dynamic from "next/dynamic";
import { ChevronDown, ChevronUp, RotateCw } from "lucide-react";
import {
  Badge,
  Button,
  EmptyState,
  SegmentedControl,
  Switch,
} from "@/components/ds";
import { fill } from "@/components/viewer/analysis-ui";
import { SeriesSwatch } from "@/components/viewer/series-swatch";
import { useLocale } from "@/components/levi-locale";
import { useTime } from "@/context/time-context";
import { indexAt } from "@/lib/replay/clock";
import type {
  Fr3GripperKind,
  Fr3ReplayDetection,
} from "@/lib/replay/fr3-columns";
import { buildReplayData, type ReplayData } from "@/lib/replay/frames";
import type { ReplayVideoSource } from "@/lib/replay/playback-clock";
import { ROBOTIQ_STROKE_M } from "@/lib/robot-studio/robotiq-2f85";
import type { Fr3View } from "./fr3-model";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import "./replay-panel.css";

// While the chunk loads the viewport shows the "Loading" line of the panel.
const ReplayScene = dynamic(() => import("./replay-scene"), {
  ssr: false,
  loading: () => null,
});

const OPEN_KEY = "replay3dOpen";
const PREFS_KEY = "replay3dPrefs";
const VIEWS: readonly Fr3View[] = ["perspective", "front", "top"];

interface Prefs {
  action: boolean;
  trail: boolean;
  view: Fr3View;
}
const DEFAULT_PREFS: Prefs = {
  action: false,
  trail: false,
  view: "perspective",
};

/** The toggles and the view, kept for the session so browsing episodes does not reset them. */
function readPrefs(): Prefs {
  try {
    const parsed = JSON.parse(
      readBrowserStorage("session", PREFS_KEY) ?? "null",
    );
    if (!parsed || typeof parsed !== "object") return DEFAULT_PREFS;
    return {
      action: parsed.action === true,
      trail: parsed.trail === true,
      view: VIEWS.includes(parsed.view) ? parsed.view : DEFAULT_PREFS.view,
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

/** WebGL or a render error in the scene shows the retry state instead of an empty page. */
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

/**
 * The panel's own controls handle Space and the arrow keys (a switch toggles,
 * the view buttons move); the page's shortcuts (play/pause, previous and next
 * episode) must not fire as well.
 */
function keepKeysLocal(event: KeyboardEvent) {
  if (event.key === " " || event.key === "ArrowUp" || event.key === "ArrowDown")
    event.stopPropagation();
}

/**
 * What the gripper column is called. Only a measured width and LEVI's own
 * command (open = 1) say more than "a number"; any other column (a DROID view's
 * measured `gripper`, whose direction is the opposite) is shown as the bare
 * value, with no claim about what it means or which way is open.
 */
const GRIPPER_TEXT: Record<Fr3GripperKind, { bar: string; value: string }> = {
  width: {
    bar: "viewer.replay3d.gripperWidthBar",
    value: "viewer.replay3d.gripperWidth",
  },
  command: {
    bar: "viewer.replay3d.gripperBar",
    value: "viewer.replay3d.gripper",
  },
  unknown: {
    bar: "viewer.replay3d.gripperUnknownBar",
    value: "viewer.replay3d.gripperUnknown",
  },
};

type SceneStatus = "loading" | "ready" | "failed";
const DEG = 180 / Math.PI;

const fixed = (
  values: ArrayLike<number>,
  from: number,
  count: number,
  digits: number,
) =>
  Array.from({ length: count }, (_, i) => {
    const v = values[from + i];
    return Number.isFinite(v) ? v.toFixed(digits) : "–";
  }).join(", ");

export interface ReplayPanelProps {
  detection: Fr3ReplayDetection;
  /** The episode's `flatChartData`. */
  rows: readonly Record<string, number>[];
  /** The primary video, from `SimpleVideosPlayer`'s `onPrimaryVideo`; keep it stable. */
  video: ReplayVideoSource | null;
}

export default function ReplayPanel({
  detection,
  rows,
  video,
}: ReplayPanelProps) {
  const { t } = useLocale();
  const { currentTime, subscribe } = useTime();
  const data = useMemo(
    () => buildReplayData(detection, rows),
    [detection, rows],
  );
  // Open by default in both modes (the 3D view is meant to sit beside the
  // video); a choice the user made is kept for the session.
  const [open, setOpen] = useState(
    () => readBrowserStorage("session", OPEN_KEY) !== "0",
  );
  // The time the scene starts at when the panel is opened.
  const timeNow = useRef(currentTime);
  timeNow.current = currentTime;
  const [openedAt, setOpenedAt] = useState(currentTime);
  const toggle = useCallback(() => {
    const next = !open;
    writeBrowserStorage("session", OPEN_KEY, next ? "1" : "0");
    setOpen(next);
    setOpenedAt(timeNow.current);
    setStatus("loading");
  }, [open]);

  const [prefs, setPrefs] = useState(readPrefs);
  const [viewRevision, setViewRevision] = useState(0);
  const changePrefs = useCallback(
    (change: Partial<Prefs>) => {
      const next = { ...prefs, ...change };
      writeBrowserStorage("session", PREFS_KEY, JSON.stringify(next));
      setPrefs(next);
    },
    [prefs],
  );
  const [status, setStatus] = useState<SceneStatus>("loading");
  const onLoad = useCallback(
    (ok: boolean) => setStatus(ok ? "ready" : "failed"),
    [],
  );
  const onFailure = useCallback(() => setStatus("failed"), []);
  // While failed the scene is not rendered, so loading again mounts it afresh.
  const retry = useCallback(() => setStatus("loading"), []);
  const titleId = useId();
  const bodyId = useId();
  const readoutId = useId();

  if (!data) {
    return (
      <section className="vw-card vw-r3d" aria-labelledby={titleId}>
        <h2 id={titleId} className="vw-label">
          {t("viewer.replay3d.title")}
        </h2>
        <p className="vw-r3d-note">{t("viewer.replay3d.empty")}</p>
      </section>
    );
  }

  const joints = data.mode === "joints";
  const row = indexAt(currentTime, data.state.t);
  const width = data.state.width;
  const at = row * width;
  const gripperRow = data.gripper ? indexAt(currentTime, data.gripper.t) : -1;
  const gripper =
    data.gripper && gripperRow >= 0 ? data.gripper.values[gripperRow] : null;
  // The bar is 0..1: the command as it is, a measured width against the
  // Robotiq 2F-85's full stroke (the only gripper the studio models).
  const measuredGripper = data.gripperKind === "width";
  const gripperText = GRIPPER_TEXT[data.gripperKind ?? "unknown"];
  const clampedGripper =
    gripper !== null && Number.isFinite(gripper)
      ? Math.min(
          1,
          Math.max(0, measuredGripper ? gripper / ROBOTIQ_STROKE_M : gripper),
        )
      : null;

  return (
    <section className="vw-card vw-r3d" aria-labelledby={titleId}>
      <div className="vw-r3d-head">
        <h2 id={titleId} className="vw-label">
          {t("viewer.replay3d.title")}
        </h2>
        <Badge tone="neutral" icon={null}>
          {t(
            joints
              ? "viewer.replay3d.mode.joints"
              : "viewer.replay3d.mode.pose",
          )}
        </Badge>
        <span className="vw-r3d-spacer" />
        <Button
          size="sm"
          variant="ghost"
          icon={open ? ChevronUp : ChevronDown}
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={toggle}
        >
          {t(open ? "viewer.replay3d.hide" : "viewer.replay3d.show")}
        </Button>
      </div>
      {open && (
        <div id={bodyId} className="vw-r3d-body">
          <div
            className="vw-r3d-viewport"
            data-theme="dark"
            role="group"
            aria-label={t(
              joints
                ? "viewer.replay3d.viewportJoints"
                : "viewer.replay3d.viewportPose",
            )}
            aria-describedby={readoutId}
          >
            {status === "failed" ? (
              <div role="alert">
                <EmptyState
                  title={t("viewer.replay3d.errorTitle")}
                  description={t("viewer.replay3d.errorBody")}
                  action={
                    <Button variant="primary" icon={RotateCw} onClick={retry}>
                      {t("viewer.replay3d.retry")}
                    </Button>
                  }
                />
              </div>
            ) : (
              <SceneBoundary onFailure={onFailure}>
                <ReplayScene
                  data={data}
                  showAction={prefs.action && data.actionMode !== "none"}
                  trail={prefs.trail}
                  view={prefs.view}
                  viewRevision={viewRevision}
                  subscribe={subscribe}
                  video={video}
                  initialTime={openedAt}
                  onLoad={onLoad}
                />
              </SceneBoundary>
            )}
            {status === "loading" && joints && (
              <p className="vw-r3d-status vw-r3d-status--over" role="status">
                {t("viewer.replay3d.loading")}
              </p>
            )}
          </div>
          <div className="vw-r3d-controls" onKeyDown={keepKeysLocal}>
            <SegmentedControl
              size="sm"
              label={t("viewer.replay3d.viewLabel")}
              value={prefs.view}
              onChange={(value) => {
                changePrefs({ view: value as Fr3View });
                setViewRevision((revision) => revision + 1);
              }}
              options={[
                {
                  value: "perspective",
                  label: t("viewer.replay3d.viewPerspective"),
                },
                { value: "front", label: t("viewer.replay3d.viewFront") },
                { value: "top", label: t("viewer.replay3d.viewTop") },
              ]}
            />
            {data.actionMode !== "none" && (
              <Switch
                label={t("viewer.replay3d.showAction")}
                checked={prefs.action}
                onChange={(event) =>
                  changePrefs({ action: event.target.checked })
                }
              />
            )}
            <Switch
              label={t("viewer.replay3d.trail")}
              checked={prefs.trail}
              onChange={(event) => changePrefs({ trail: event.target.checked })}
            />
          </div>
          <ReplayLegend
            data={data}
            showAction={prefs.action && data.actionMode !== "none"}
            trail={prefs.trail}
          />
          <div id={readoutId} className="vw-r3d-readout">
            <p>
              {fill(t("viewer.replay3d.frame"), {
                row: row + 1,
                rows: data.rows,
                time: currentTime.toFixed(2),
              })}
            </p>
            {joints ? (
              <p>
                {fill(t("viewer.replay3d.readoutJoints"), {
                  values: Array.from({ length: 7 }, (_, c) => {
                    const v = data.state.values[at + c] * DEG;
                    return Number.isFinite(v) ? v.toFixed(1) : "–";
                  }).join(", "),
                })}
              </p>
            ) : (
              <>
                <p>
                  {fill(t("viewer.replay3d.readoutPosition"), {
                    values: fixed(data.state.values, at, 3, 3),
                  })}
                </p>
                <p>
                  {data.rotation === "quaternion"
                    ? fill(t("viewer.replay3d.readoutQuaternion"), {
                        values: fixed(data.state.values, at + 3, 4, 3),
                      })
                    : fill(t("viewer.replay3d.readoutEuler"), {
                        values: fixed(data.state.values, at + 3, 3, 2),
                      })}
                </p>
              </>
            )}
          </div>
          {data.gripper && (
            <div className="vw-r3d-gripper">
              <div
                className="vw-r3d-gripper-bar"
                // A meter needs a value (aria-valuenow is required): with
                // none, the bar is empty decoration and the text beside it
                // says "–".
                {...(clampedGripper === null
                  ? { "aria-hidden": true }
                  : {
                      role: "meter",
                      "aria-label": t(gripperText.bar),
                      "aria-valuemin": 0,
                      "aria-valuemax": 1,
                      "aria-valuenow": clampedGripper,
                    })}
                style={
                  { "--vw-r3d-grip": clampedGripper ?? 0 } as CSSProperties
                }
              />
              <p>
                {fill(t(gripperText.value), {
                  value:
                    gripper !== null && Number.isFinite(gripper)
                      ? gripper.toFixed(measuredGripper ? 3 : 2)
                      : "–",
                })}
              </p>
            </div>
          )}
          <ReplayNotes data={data} />
        </div>
      )}
    </section>
  );
}

function ReplayLegend({
  data,
  showAction,
  trail,
}: {
  data: ReplayData;
  showAction: boolean;
  trail: boolean;
}) {
  const { t } = useLocale();
  const joints = data.mode === "joints";
  return (
    <ul className="vw-r3d-legend" aria-label={t("viewer.replay3d.legendLabel")}>
      <li>
        <SeriesSwatch index={0} />
        {t(
          joints
            ? "viewer.replay3d.legendState"
            : "viewer.replay3d.legendStatePose",
        )}
      </li>
      {showAction && (
        <>
          <li>
            <SeriesSwatch index={1} />
            {t(
              data.actionMode === "joints"
                ? "viewer.replay3d.legendAction"
                : "viewer.replay3d.legendActionPose",
            )}
          </li>
          <li>{t("viewer.replay3d.legendNextState")}</li>
        </>
      )}
      {trail && <li>{t("viewer.replay3d.legendTrail")}</li>}
    </ul>
  );
}

function ReplayNotes({ data }: { data: ReplayData }) {
  const { t } = useLocale();
  return (
    <ul className="vw-r3d-notes">
      {data.mode === "pose" && <li>{t("viewer.replay3d.poseNote")}</li>}
      {data.gripper && <li>{t("viewer.replay3d.noGripperModel")}</li>}
      {data.gripperKind === "unknown" && (
        <li>{t("viewer.replay3d.gripperUnknownNote")}</li>
      )}
      {data.maybeSampled && (
        <li>{fill(t("viewer.replay3d.sampled"), { rows: data.rows })}</li>
      )}
      {data.warnings.map((warning) =>
        warning.kind === "joints-clamped" ? (
          <li key={`clamp-${warning.track}`}>
            {fill(
              t(
                warning.track === "state"
                  ? "viewer.replay3d.warnClampedState"
                  : "viewer.replay3d.warnClampedAction",
              ),
              { rows: warning.rows, far: warning.farRows },
            )}
          </li>
        ) : (
          <li key="missing">
            {fill(t("viewer.replay3d.warnMissing"), { rows: warning.rows })}
          </li>
        ),
      )}
    </ul>
  );
}
