"use client";
// The live view under the Run card (step 4): the newest picture of the side
// (third-person) camera on the left and of the wrist camera on the right, of
// the episode the page's client is recording. The client owns both cameras, so
// the pictures are the last complete frame of the raw video file it writes
// while an episode runs (levi/marvel0/live_frame.py): about ten a second at
// the source, asked for about ten times a second here (2026-11-02: four a
// second looked jerky on the robot), and none between episodes. Nothing here opens a camera, connects to a robot-side port or
// sends the client anything; it only reads LEVI's own route through the
// same-origin proxy.
//
// Rules of the poll: only while the run is active and the tab is visible; at
// most one request in flight per camera (a slow answer skips ticks, it never
// piles requests up); every picture is an object URL that is revoked when the
// next one replaces it, when the run ends and when the card goes away; a
// missing picture leaves the last one on screen for a moment (the gap between
// two frames of one episode, or the client's pause) and then says why.
import { useEffect, useState } from "react";
import { Card } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  FRAME_CAMERAS,
  runApi,
  type FrameCamera,
  type FrameReason,
  type FrameResult,
  type RunApi,
} from "./run-api";

/** Milliseconds between two asks of a camera (about ten a second, the rate the
 * client writes frames at). */
export const LIVE_POLL_MS = 100;
/** How long the last picture stays when the next one is missing. */
export const LIVE_KEEP_MS = 3_000;
/** A picture older than this is marked: the client has stopped writing. */
export const LIVE_LATE_MS = 5_000;

interface Shot {
  url: string | null;
  ageMs: number | null;
  episode: string | null;
  /** Why the newest ask had no picture; null while it had one. */
  reason: FrameReason | null;
}
const NO_SHOT: Shot = { url: null, ageMs: null, episode: null, reason: null };
type Shots = Record<FrameCamera, Shot>;
const NO_SHOTS: Shots = { side: NO_SHOT, wrist: NO_SHOT };

export function LiveFrames({
  active,
  api = runApi,
  pollMs = LIVE_POLL_MS,
  keepMs = LIVE_KEEP_MS,
}: {
  /** The page's run has a client (run-logic.ts `hasPictures`). */
  active: boolean;
  api?: Pick<RunApi, "frame">;
  pollMs?: number;
  keepMs?: number;
}) {
  const { t } = useLocale();
  const [shots, setShots] = useState<Shots>(NO_SHOTS);

  useEffect(() => {
    if (!active) return;
    let stopped = false;
    const urls: Record<FrameCamera, string | null> = {
      side: null,
      wrist: null,
    };
    const lastOk: Record<FrameCamera, number> = { side: 0, wrist: 0 };
    const asking: Record<FrameCamera, AbortController | null> = {
      side: null,
      wrist: null,
    };
    const dropTimer: Record<FrameCamera, ReturnType<typeof setTimeout> | null> =
      { side: null, wrist: null };
    const visible = () => document.visibilityState !== "hidden";
    const patch = (camera: FrameCamera, change: Partial<Shot>) =>
      setShots((all) => ({ ...all, [camera]: { ...all[camera], ...change } }));
    const cancelDrop = (camera: FrameCamera) => {
      const timer = dropTimer[camera];
      if (timer !== null) clearTimeout(timer);
      dropTimer[camera] = null;
    };
    const forget = (camera: FrameCamera) => {
      cancelDrop(camera);
      const url = urls[camera];
      if (url !== null) URL.revokeObjectURL(url);
      urls[camera] = null;
    };

    const take = (camera: FrameCamera, result: FrameResult) => {
      if (result.ok) {
        const next = URL.createObjectURL(result.blob);
        const previous = urls[camera];
        urls[camera] = next;
        cancelDrop(camera);
        lastOk[camera] = Date.now();
        patch(camera, {
          url: next,
          ageMs: result.ageMs,
          episode: result.episode,
          reason: null,
        });
        if (previous !== null) URL.revokeObjectURL(previous);
        return;
      }
      patch(camera, { reason: result.reason });
      if (urls[camera] === null || dropTimer[camera] !== null) return;
      // Keep the last picture a moment, then let go of it.
      dropTimer[camera] = setTimeout(
        () => {
          dropTimer[camera] = null;
          forget(camera);
          patch(camera, { url: null, ageMs: null, episode: null });
        },
        Math.max(0, keepMs - (Date.now() - lastOk[camera])),
      );
    };

    const ask = async (camera: FrameCamera) => {
      if (stopped || asking[camera] !== null || !visible()) return;
      const controller = new AbortController();
      asking[camera] = controller;
      let result: FrameResult | null = null;
      try {
        result = await api.frame(camera, controller.signal);
      } catch {
        result = null; // aborted
      }
      if (asking[camera] === controller) asking[camera] = null;
      if (stopped || controller.signal.aborted || result === null) return;
      take(camera, result);
    };
    const askAll = () => {
      for (const camera of FRAME_CAMERAS) void ask(camera);
    };
    const onVisibility = () => {
      if (visible()) askAll();
      else
        for (const camera of FRAME_CAMERAS) {
          asking[camera]?.abort();
          asking[camera] = null;
        }
    };

    document.addEventListener("visibilitychange", onVisibility);
    const timer = setInterval(askAll, pollMs);
    askAll();
    return () => {
      stopped = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      for (const camera of FRAME_CAMERAS) {
        asking[camera]?.abort();
        forget(camera);
      }
      setShots(NO_SHOTS);
    };
  }, [active, api, pollMs, keepMs]);

  const shown = active ? shots : NO_SHOTS;
  const label: Record<FrameCamera, string> = {
    side: t("marvel0.live.side"),
    wrist: t("marvel0.live.wrist"),
  };
  const alt: Record<FrameCamera, string> = {
    side: t("marvel0.live.alt.side"),
    wrist: t("marvel0.live.alt.wrist"),
  };
  const placeholder = (reason: FrameReason | null) =>
    !active || reason === "no_run"
      ? t("marvel0.live.no_run")
      : reason === "no_episode"
        ? t("marvel0.live.no_episode")
        : reason === "no_frame"
          ? t("marvel0.live.no_frame")
          : reason === "unavailable"
            ? t("marvel0.live.unavailable")
            : t("marvel0.live.wait");
  const ageText = (ms: number) => {
    const seconds = ms / 1000;
    return t("marvel0.live.age").replace(
      "{age}",
      seconds < 10 ? seconds.toFixed(1) : String(Math.round(seconds)),
    );
  };

  return (
    <Card
      padding="compact"
      className="rg-card rg-card--not_started rg-live"
      data-testid="marvel0-live"
      data-active={active ? "true" : "false"}
      title={t("marvel0.live.title")}
      description={t("marvel0.live.lead")}
    >
      <div className="rg-live__grid">
        {FRAME_CAMERAS.map((camera) => {
          const shot = shown[camera];
          const late = shot.ageMs !== null && shot.ageMs > LIVE_LATE_MS;
          return (
            <figure
              key={camera}
              className="rg-live__cam"
              data-testid={`marvel0-live-${camera}`}
            >
              <figcaption className="rg-live__label">
                {label[camera]}
              </figcaption>
              <div
                className="rg-live__box ds-on-sunken"
                data-shown={shot.url !== null ? "true" : "false"}
              >
                {shot.url !== null ? (
                  // eslint-disable-next-line @next/next/no-img-element -- a blob: URL of a camera frame, not a file Next can optimise
                  <img
                    className="rg-live__img"
                    src={shot.url}
                    alt={alt[camera]}
                  />
                ) : (
                  <p
                    className="rg-live__empty"
                    data-testid={`marvel0-live-${camera}-empty`}
                    data-reason={active ? (shot.reason ?? "wait") : "no_run"}
                  >
                    {placeholder(shot.reason)}
                  </p>
                )}
              </div>
              <p
                className="rg-live__meta"
                data-late={late ? "true" : "false"}
                data-testid={`marvel0-live-${camera}-meta`}
              >
                {shot.url !== null && shot.reason === null
                  ? [
                      shot.ageMs !== null ? ageText(shot.ageMs) : null,
                      shot.episode,
                    ]
                      .filter(Boolean)
                      .join(" · ")
                  : ""}
              </p>
            </figure>
          );
        })}
      </div>
    </Card>
  );
}
