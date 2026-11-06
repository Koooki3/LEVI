"use client";
// The 3D replay panel of the Episodes tab: beside the video(s) on a wide
// screen, below them on a narrow one. Joints mode draws the FR3; pose mode
// (no joint columns) draws only the end-effector path and axes, and says the
// pose frame is not verified. The scene itself (three.js, React Three Fiber)
// is loaded on demand and only when the panel is open.
import {
  useCallback,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import dynamic from "next/dynamic";
import { ChevronDown, ChevronUp } from "lucide-react";
import { Badge, Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { useTime } from "@/context/time-context";
import { indexAt } from "@/lib/replay/clock";
import type { Fr3ReplayDetection } from "@/lib/replay/fr3-columns";
import { buildReplayData, type ReplayData } from "@/lib/replay/frames";
import type { ReplayVideoSource } from "@/lib/replay/playback-clock";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";
import "./replay-panel.css";

const ReplayScene = dynamic(() => import("./replay-scene"), {
  ssr: false,
  loading: () => <SceneLoading />,
});

const OPEN_KEY = "replay3dOpen";
const ignoreLoad = () => {};
const DEG = 180 / Math.PI;

function SceneLoading() {
  const { t } = useLocale();
  return (
    <p className="vw-r3d-status" role="status">
      {t("viewer.replay3d.loading")}
    </p>
  );
}

/** "Row {row}" -> "Row 3": fills the named holes of a catalogue sentence. */
export function fill(
  template: string,
  values: Record<string, string | number>,
): string {
  return template.replace(/\{(\w+)\}/g, (hole, name: string) =>
    name in values ? String(values[name]) : hole,
  );
}

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
  /** `fr3`: the robot_type names the FR3 family (open by default); `generic`: pose only. */
  family: "fr3" | "generic";
  /** The primary video, from `SimpleVideosPlayer`'s `onPrimaryVideo`; keep it stable. */
  video: ReplayVideoSource | null;
}

export default function ReplayPanel({
  detection,
  rows,
  family,
  video,
}: ReplayPanelProps) {
  const { t } = useLocale();
  const { currentTime, subscribe } = useTime();
  const data = useMemo(
    () => buildReplayData(detection, rows),
    [detection, rows],
  );
  const [open, setOpen] = useState(() => {
    const stored = readBrowserStorage("session", OPEN_KEY);
    return stored === null ? family === "fr3" : stored === "1";
  });
  // The time the scene starts at when the panel is opened.
  const timeNow = useRef(currentTime);
  timeNow.current = currentTime;
  const [openedAt, setOpenedAt] = useState(currentTime);
  const toggle = useCallback(() => {
    const next = !open;
    writeBrowserStorage("session", OPEN_KEY, next ? "1" : "0");
    setOpen(next);
    setOpenedAt(timeNow.current);
  }, [open]);

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
  const clampedGripper =
    gripper !== null && Number.isFinite(gripper)
      ? Math.min(1, Math.max(0, gripper))
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
            <ReplayScene
              data={data}
              showAction={false}
              trail={false}
              view="perspective"
              viewRevision={0}
              subscribe={subscribe}
              video={video}
              initialTime={openedAt}
              onLoad={ignoreLoad}
            />
          </div>
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
                role="meter"
                aria-label={t("viewer.replay3d.gripperBar")}
                aria-valuemin={0}
                aria-valuemax={1}
                aria-valuenow={clampedGripper ?? undefined}
                style={
                  { "--vw-r3d-grip": clampedGripper ?? 0 } as CSSProperties
                }
              />
              <p>
                {fill(t("viewer.replay3d.gripper"), {
                  value:
                    gripper !== null && Number.isFinite(gripper)
                      ? gripper.toFixed(2)
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

function ReplayNotes({ data }: { data: ReplayData }) {
  const { t } = useLocale();
  return (
    <ul className="vw-r3d-notes">
      {data.mode === "pose" && <li>{t("viewer.replay3d.poseNote")}</li>}
      {data.gripper && <li>{t("viewer.replay3d.noGripperModel")}</li>}
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
