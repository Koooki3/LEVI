"use client";

/**
 * VALUE MODEL section of the annotations timeline: RECAP advantage labels
 * from a value-model checkpoint (backend `recap/*` routes).
 *
 *   ADVANTAGE — runs of frames with the same label; green = positive, red =
 *               negative, opacity by |A − threshold| (normalised per episode).
 *   VALUE     — V(o_t) in [-1, 0] as a polyline (0 at the top), with a dot at
 *               the playhead and the value/advantage of that frame.
 *
 * Rendered inside `.tl-tracks` after the language sections so the timeline's
 * playhead spans it; rows reuse `.tl-row` so the tracks line up exactly. The
 * section loads on its own and never blocks the rows above it: it starts as a
 * one-line header and only grows once there is something to draw.
 */

import React, { useCallback, useEffect, useMemo, useState } from "react";
import { T, useLocale } from "@/components/levi-locale";
import { useAnnotations } from "@/context/annotations-context";
import {
  advantageRuns,
  formatSigned,
  nearestFrame,
  positiveFraction,
  recapView,
  valuePath,
} from "@/components/recap-lanes";
import {
  cancelRecapJob,
  fetchRecapEpisode,
  fetchRecapJob,
  fetchRecapStatus,
  isAnnotateBackendEnabled,
  runRecap,
} from "@/utils/annotationsClient";
import {
  RECAP_UPDATED_EVENT,
  isRecapJobActive,
  type RecapEpisode,
  type RecapJob,
  type RecapStatus,
} from "@/types/recap.types";

const POLL_MS = 1500;
/** viewBox of the value plot; stretched to the track with
 * preserveAspectRatio="none" (strokes stay 1.5px via non-scaling-stroke). */
const PLOT_W = 1000;
const PLOT_H = 100;

interface Props {
  duration: number;
  currentTime: number;
  onSeek: (t: number) => void;
  onBandClick: (e: React.MouseEvent) => void;
  onHoverMove: (e: React.MouseEvent) => void;
  onHoverLeave: () => void;
  showTip: (e: React.MouseEvent, meta: string, text: string) => void;
  moveTip: (e: React.MouseEvent) => void;
  hideTip: () => void;
}

const isAbort = (error: unknown) =>
  error instanceof DOMException && error.name === "AbortError";

const message = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

const percent = (fraction: number | null | undefined) =>
  fraction == null || !Number.isFinite(fraction)
    ? "—"
    : `${Math.round(fraction * 100)}%`;

export const RecapValueSection: React.FC<Props> = ({
  duration,
  currentTime,
  onSeek,
  onBandClick,
  onHoverMove,
  onHoverLeave,
  showTip,
  moveTip,
  hideTip,
}) => {
  const { episodeId, ident } = useAnnotations();
  const { t } = useLocale();
  const repoId = ident.repoId ?? null;

  const [status, setStatus] = useState<RecapStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  // undefined = loading, null = no labels for this episode.
  const [episode, setEpisode] = useState<RecapEpisode | null | undefined>(
    undefined,
  );
  const [job, setJob] = useState<RecapJob | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [checkpoint, setCheckpoint] = useState<string>("");
  const [showControls, setShowControls] = useState(false);
  const [starting, setStarting] = useState(false);
  // Bumped to refetch status + episode (after a run finishes).
  const [reloadKey, setReloadKey] = useState(0);

  const enabled = isAnnotateBackendEnabled() && !!repoId && episodeId != null;

  // ---- Load status + this episode's labels; abort on episode change ----
  useEffect(() => {
    if (!enabled || !repoId || episodeId == null) return;
    const controller = new AbortController();
    const target = { repoId };
    setEpisode(undefined);
    fetchRecapStatus(target, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setStatus(next);
        setStatusError(null);
        setJob((current) =>
          next.job &&
          (isRecapJobActive(next.job) || current?.id === next.job.id)
            ? next.job
            : isRecapJobActive(current)
              ? current
              : null,
        );
      })
      .catch((error) => {
        if (!isAbort(error)) setStatusError(message(error));
      });
    fetchRecapEpisode(episodeId, target, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setEpisode(next);
      })
      .catch((error) => {
        if (!isAbort(error) && !controller.signal.aborted) setEpisode(null);
      });
    return () => controller.abort();
  }, [enabled, repoId, episodeId, reloadKey]);

  // Default the checkpoint picker to the current result's checkpoint, else
  // the first ready one.
  const readyCheckpoints = useMemo(
    () => (status?.checkpoints ?? []).filter((c) => c.ready),
    [status],
  );
  useEffect(() => {
    if (!status) return;
    setCheckpoint((selected) => {
      if (readyCheckpoints.some((c) => c.name === selected)) return selected;
      const current = status.current?.checkpoint;
      if (current && readyCheckpoints.some((c) => c.name === current))
        return current;
      return readyCheckpoints[0]?.name ?? "";
    });
  }, [status, readyCheckpoints]);

  // ---- Poll an active job every 1.5 s ----
  const activeJobId = isRecapJobActive(job) ? job?.id : undefined;
  useEffect(() => {
    if (!activeJobId || !repoId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = () => {
      fetchRecapJob(activeJobId, { repoId }, controller.signal)
        .then((next) => {
          if (controller.signal.aborted) return;
          setJob(next);
          if (isRecapJobActive(next)) {
            timer = setTimeout(poll, POLL_MS);
            return;
          }
          if (next.status === "succeeded") {
            setShowControls(false);
            setReloadKey((k) => k + 1);
            window.dispatchEvent(new CustomEvent(RECAP_UPDATED_EVENT));
          } else if (next.status === "failed") {
            setRunError(next.error || t("Advantage computation failed"));
          }
        })
        .catch((error) => {
          if (isAbort(error)) return;
          // A transient proxy error should not end the progress display.
          timer = setTimeout(poll, POLL_MS * 2);
        });
    };
    timer = setTimeout(poll, POLL_MS);
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
    // `t` only formats the failure message; re-polling on a language switch
    // would be wasteful.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeJobId, repoId]);

  const startRun = useCallback(async () => {
    if (!repoId || !checkpoint) return;
    setRunError(null);
    setStarting(true);
    try {
      setJob(await runRecap({ repoId }, { checkpoint }));
    } catch (error) {
      setRunError(message(error));
    } finally {
      setStarting(false);
    }
  }, [repoId, checkpoint]);

  const cancelRun = useCallback(async () => {
    if (!job || !repoId) return;
    try {
      setJob(await cancelRecapJob(job.id, { repoId }));
    } catch (error) {
      setRunError(message(error));
    }
  }, [job, repoId]);

  // ---- Geometry ----
  const runs = useMemo(
    () =>
      episode
        ? advantageRuns(
            episode.frame_index,
            episode.positive,
            episode.advantage,
            episode.timestamp,
            episode.fps,
            episode.threshold,
          )
        : [],
    [episode],
  );
  const points = useMemo(
    () =>
      episode && duration > 0
        ? valuePath(episode.value, episode.timestamp, duration, PLOT_W, PLOT_H)
        : "",
    [episode, duration],
  );
  const episodeFraction = useMemo(
    () => (episode ? positiveFraction(episode.positive) : null),
    [episode],
  );
  const frameAtPlayhead = episode
    ? nearestFrame(episode.timestamp, currentTime)
    : -1;

  if (!enabled) return null;

  const current = status?.current ?? null;
  const jobActive = isRecapJobActive(job);
  const hasCheckpoints = (status?.checkpoints.length ?? 0) > 0;
  const view = recapView({
    statusLoaded: !!status,
    statusError: !!statusError,
    hasCheckpoints,
    hasCurrent: !!current,
    jobActive,
    episode:
      episode === undefined
        ? "loading"
        : episode && episode.timestamp.length > 0
          ? "labels"
          : "none",
    controlsOpen: showControls,
  });

  // ---- Header pieces ----
  const legend = (
    <span className="recap-legend">
      <span className="recap-swatch pos" />
      <T>positive</T>
      <span className="recap-swatch neg" />
      <T>negative</T>
    </span>
  );

  const progress = job && jobActive && (
    <span className="recap-job">
      <span className="recap-job-stage">
        <T>{job.status === "queued" ? "queued" : job.progress.stage}</T>
        {job.progress.total > 0 &&
          ` ${job.progress.done}/${job.progress.total}`}
      </span>
      <span
        className="recap-progress"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={job.progress.total || 1}
        aria-valuenow={job.progress.done}
      >
        <span
          style={{
            width: `${
              job.progress.total > 0
                ? Math.min(100, (100 * job.progress.done) / job.progress.total)
                : 0
            }%`,
          }}
        />
      </span>
      <button type="button" className="recap-btn" onClick={cancelRun}>
        <T>Cancel</T>
      </button>
    </span>
  );

  const workerReason =
    status && !status.worker.ready
      ? status.worker.reason || t("The advantage worker is not ready")
      : null;

  const computeControl = status && !jobActive && (
    <span className="recap-compute">
      <select
        aria-label={t("Value-model checkpoint")}
        value={checkpoint}
        onChange={(e) => setCheckpoint(e.target.value)}
        disabled={readyCheckpoints.length === 0}
      >
        {readyCheckpoints.length === 0 && (
          <option value="">{t("No ready checkpoint")}</option>
        )}
        {status.checkpoints.map((c) => (
          <option
            key={c.name}
            value={c.name}
            disabled={!c.ready}
            title={c.reason ?? c.notes ?? undefined}
          >
            {`${c.name}${c.provider === "fake" ? " (fake)" : ""}${
              c.ready ? "" : ` — ${t("not ready")}`
            }`}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="recap-btn primary"
        onClick={startRun}
        disabled={!checkpoint || starting || !!workerReason}
        title={workerReason ?? undefined}
      >
        {t(current ? "Recompute" : "Compute advantages")}
      </button>
      {current && (
        <button
          type="button"
          className="recap-btn"
          onClick={() => {
            setShowControls(false);
            setRunError(null);
          }}
        >
          <T>Close</T>
        </button>
      )}
    </span>
  );

  const errorLine = runError ? (
    <div className="recap-note error" role="alert">
      {runError}
    </div>
  ) : job?.status === "cancelled" ? (
    <div className="recap-note">
      <T>Advantage computation cancelled</T>
    </div>
  ) : null;

  // States without rows collapse to the header line alone: a short muted
  // note in place of the subtitle.
  const note: React.ReactNode =
    view.note === "unavailable" ? (
      <>
        <T>Value model unavailable</T>: {statusError}
      </>
    ) : view.note === "loading" ? (
      <T>Loading…</T>
    ) : view.note === "no-checkpoint" ? (
      <>
        <T>No value-model checkpoint yet — add one with</T>{" "}
        <code>levi recap import</code>
      </>
    ) : view.note === "no-episode-labels" ? (
      <T>No advantage labels for this episode</T>
    ) : null;

  const head = (
    <div className="tl-section-head value-model">
      <span className="tl-section-title">
        <T>Value model</T>
      </span>
      {view.showMeta && episode ? (
        <span className="tl-section-sub recap-meta">
          <span title={t("Value-model checkpoint")}>
            {current?.checkpoint ?? episode.revision_id}
          </span>
          <span>
            {"· "}
            <T>threshold</T> {formatSigned(episode.threshold, 4)}
          </span>
          <span
            title={t("Share of this episode's frames with positive advantage")}
          >
            {"· "}
            {percent(episodeFraction)} <T>positive frames</T>
          </span>
          {current?.stale && (
            <span
              className="recap-stale"
              title={t(
                "The dataset changed after these labels were computed — recompute to refresh them",
              )}
            >
              <T>stale</T>
            </span>
          )}
        </span>
      ) : (
        <span className="tl-section-sub">
          {note ?? <T>RECAP advantage labels</T>}
        </span>
      )}
      <span className="recap-head-right">
        {view.showMeta && legend}
        {view.control === "progress" && progress}
        {view.control === "recompute" && (
          <button
            type="button"
            className="recap-btn ghost"
            onClick={() => setShowControls(true)}
            title={t("Compute the advantage labels again")}
          >
            <T>Recompute</T>
          </button>
        )}
        {view.control === "compute" && computeControl}
      </span>
    </div>
  );

  return (
    <T>
      <div className="tl-section recap-section">
        {head}
        {workerReason && view.control === "compute" && (
          <div className="recap-note">{workerReason}</div>
        )}
        {errorLine}
        {view.showRows && (
          <>
            <div className="tl-row">
              <div className="label">
                <span className="style-dot dot-advantage" />
                <T>advantage</T>
              </div>
              <div
                className="track"
                onClick={onBandClick}
                onMouseMove={onHoverMove}
                onMouseLeave={onHoverLeave}
              >
                {runs.map((run, k) => {
                  // Labels past the video's end (a longer data stream) have
                  // nowhere to go on the track.
                  if (run.start >= duration) return null;
                  const left = (run.start / duration) * 100;
                  const width = Math.max(
                    0.2,
                    ((Math.min(run.end, duration) - run.start) / duration) *
                      100,
                  );
                  const label = t(
                    run.positive ? "positive advantage" : "negative advantage",
                  );
                  return (
                    <div
                      key={k}
                      className={`tl-seg adv ${run.positive ? "pos" : "neg"}`}
                      style={{
                        left: `${left}%`,
                        width: `${width}%`,
                        opacity: run.strength,
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        onSeek(run.start);
                      }}
                      onMouseEnter={(e) =>
                        showTip(
                          e,
                          `${t("advantage")} · ${run.start.toFixed(2)}s → ${run.end.toFixed(2)}s · ${run.count} ${t("frames")}`,
                          `${label} · ${t("mean A")} ${formatSigned(run.meanAdv, 4)}`,
                        )
                      }
                      onMouseMove={moveTip}
                      onMouseLeave={hideTip}
                    />
                  );
                })}
              </div>
            </div>
            <div className="tl-row recap-value-row">
              <div className="label">
                <span className="style-dot dot-value" />
                <T>value</T>
              </div>
              <div
                className="track"
                onClick={onBandClick}
                onMouseMove={onHoverMove}
                onMouseLeave={onHoverLeave}
              >
                <svg
                  className="recap-plot"
                  viewBox={`0 0 ${PLOT_W} ${PLOT_H}`}
                  preserveAspectRatio="none"
                  aria-hidden="true"
                >
                  <line
                    className="recap-grid"
                    x1={0}
                    x2={PLOT_W}
                    y1={PLOT_H / 2}
                    y2={PLOT_H / 2}
                    vectorEffect="non-scaling-stroke"
                  />
                  <polyline
                    className="recap-line"
                    points={points}
                    vectorEffect="non-scaling-stroke"
                  />
                </svg>
                <span className="recap-axis top">0</span>
                <span className="recap-axis bottom">−1</span>
                {episode &&
                  frameAtPlayhead >= 0 &&
                  Number.isFinite(episode.value[frameAtPlayhead]) &&
                  (() => {
                    const v = Math.max(
                      -1,
                      Math.min(0, episode.value[frameAtPlayhead]),
                    );
                    const x = Math.max(0, Math.min(1, currentTime / duration));
                    const a = episode.advantage[frameAtPlayhead];
                    const pos = episode.positive[frameAtPlayhead] === true;
                    return (
                      <>
                        <span
                          className={`recap-dot ${pos ? "pos" : "neg"}`}
                          style={{ left: `${x * 100}%`, top: `${-v * 100}%` }}
                        />
                        <span
                          className={`recap-readout ${x > 0.75 ? "flip" : ""} ${v > -0.5 ? "low" : "high"}`}
                          style={{ left: `${x * 100}%` }}
                        >
                          V {formatSigned(episode.value[frameAtPlayhead], 3)}
                          {" · "}A {formatSigned(a, 4)}
                        </span>
                      </>
                    );
                  })()}
              </div>
            </div>
          </>
        )}
      </div>
    </T>
  );
};
