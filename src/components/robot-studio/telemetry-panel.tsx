"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import type {
  RobotStudioMeasurement,
  RobotStudioState,
} from "@/types/robotStudio";
import {
  freshMeasurement,
  validTelemetryState,
  measuredOpeningRatio,
} from "./telemetry-state";

type Props = {
  onMeasurement: (measurement: RobotStudioMeasurement | null) => void;
  onUseMeasurement: (measurement: RobotStudioMeasurement) => void;
  onEnabledChange: (enabled: boolean) => void;
  following: boolean;
  onFollowMeasurement: (measurement: RobotStudioMeasurement | null) => void;
  canUsePreview: boolean;
};

export default function TelemetryPanel({
  onMeasurement,
  onUseMeasurement,
  onEnabledChange,
  following,
  onFollowMeasurement,
  canUsePreview,
}: Props) {
  const { t } = useLocale();
  const [enabled, setEnabled] = useState(true);
  const [state, setState] = useState<RobotStudioState | null>(null);
  const [failed, setFailed] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const received = useRef(0);
  useEffect(() => {
    if (!enabled) {
      setState(null);
      setFailed(false);
      return;
    }
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let request: AbortController | undefined;
    let inFlight = false;
    const hidden = () => document.hidden;
    const poll = async () => {
      inFlight = true;
      const requestedAt = performance.now();
      request = new AbortController();
      const timeout = setTimeout(() => request?.abort(), 1200);
      try {
        const response = await fetch("/api/levi/robot-studio/state", {
          method: "GET",
          cache: "no-store",
          signal: request.signal,
        });
        if (!response.ok) throw new Error("State unavailable");
        const payload: unknown = await response.json();
        if (!validTelemetryState(payload))
          throw new Error("Invalid state envelope");
        if (!disposed) {
          received.current = requestedAt;
          setElapsed(performance.now() - requestedAt);
          setState(payload);
          setFailed(false);
        }
      } catch {
        if (!disposed) {
          setFailed(true);
        }
      } finally {
        clearTimeout(timeout);
        inFlight = false;
        // A hidden tab neither polls nor ticks: nobody can see the page, and
        // coming back re-reads at once (below).
        if (!disposed && !hidden()) timer = setTimeout(poll, 500);
      }
    };
    void poll();
    const clock = setInterval(() => {
      if (!hidden()) setElapsed(performance.now() - received.current);
    }, 200);
    const onVisibility = () => {
      if (hidden()) {
        clearTimeout(timer);
        timer = undefined;
        return;
      }
      // Age follows the monotonic clock, so a sample from before the tab was
      // hidden is expired at once instead of looking fresh until the next poll.
      setElapsed(performance.now() - received.current);
      if (!inFlight && !disposed) {
        clearTimeout(timer);
        void poll();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimeout(timer);
      clearInterval(clock);
      request?.abort();
    };
  }, [enabled]);
  const measurement =
    enabled && !failed ? freshMeasurement(state, elapsed) : null;
  useEffect(() => {
    onMeasurement(measurement);
  }, [measurement, onMeasurement]);
  const status = !enabled
    ? "off"
    : failed
      ? "unavailable"
      : state?.state === "fresh" && !measurement
        ? "stale"
        : (state?.state ?? "loading");
  const gripper = measurement?.gripper;
  const currentMeasurement = () =>
    enabled
      ? freshMeasurement(state, performance.now() - received.current)
      : null;
  return (
    <section className="rs-panel-section rs-telemetry-panel">
      <h2>{t("robotStudio.state.title")}</h2>
      <p className="rs-meta">{t("robotStudio.state.readOnly")}</p>
      <div className="rs-state-status" role="status">
        <span className={measurement ? "rs-state-live" : "rs-state-unknown"} />
        {t("robotStudio.state." + status)}
      </div>
      {enabled && !measurement && (
        <p className="rs-meta">{t("robotStudio.state.noProducer")}</p>
      )}
      <dl className="rs-state-facts">
        <div>
          <dt>{t("robotStudio.state.source")}</dt>
          <dd>{state?.source_id || "—"}</dd>
        </div>
        <div>
          <dt>{t("robotStudio.state.age")}</dt>
          <dd>
            {typeof state?.age_s === "number"
              ? Math.max(0, state.age_s * 1000 + elapsed).toFixed(0) + " ms"
              : "—"}
          </dd>
        </div>
      </dl>
      {measurement && (
        <details>
          <summary>{t("robotStudio.state.details")}</summary>
          <dl className="rs-state-facts">
            <div>
              <dt>{t("robotStudio.state.joints")}</dt>
              <dd>
                {measurement.joints_rad
                  .map((q) => ((q * 180) / Math.PI).toFixed(1))
                  .join(" · ")}
                °
              </dd>
            </div>
            <div>
              <dt>{t("robotStudio.state.gripper")}</dt>
              <dd>
                {!gripper
                  ? "—"
                  : "opening_width_m" in gripper
                    ? (gripper.opening_width_m! * 1000).toFixed(1) + " mm"
                    : gripper.knuckle_angle_rad!.toFixed(4) + " rad"}
              </dd>
            </div>
            {measurement.reported_ee && (
              <div>
                <dt>{t("robotStudio.state.reportedFrame")}</dt>
                <dd>
                  {measurement.reported_ee.frame_id} →{" "}
                  {measurement.reported_ee.end_frame}
                </dd>
              </div>
            )}
          </dl>
        </details>
      )}
      {measurement?.reported_ee && (
        <p className="rs-meta">{t("robotStudio.state.eeSeparation")}</p>
      )}
      {measurement && measuredOpeningRatio(measurement) === null && (
        <p className="rs-meta">{t("robotStudio.state.gripperUnknown")}</p>
      )}
      <div className="rs-actions">
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            const next = !enabled;
            setEnabled(next);
            onEnabledChange(next);
          }}
        >
          {t(
            enabled
              ? "robotStudio.state.disconnect"
              : "robotStudio.state.connect",
          )}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-pressed={following}
          disabled={!enabled || following}
          onClick={() => onFollowMeasurement(currentMeasurement())}
        >
          {t("robotStudio.state.follow")}
        </Button>
        <Button
          size="sm"
          aria-pressed={enabled && !following}
          disabled={!measurement || !canUsePreview}
          onClick={() => {
            const current = currentMeasurement();
            if (current && canUsePreview) onUseMeasurement(current);
          }}
        >
          {t(
            following
              ? "robotStudio.state.editMeasured"
              : "robotStudio.state.copyPreview",
          )}
        </Button>
      </div>
      <p className="rs-meta">{t("robotStudio.state.legend")}</p>
    </section>
  );
}
