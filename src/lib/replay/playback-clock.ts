// Where the replay's time comes from (design:
// lab/levi-hub/programs/levi2/REPLAY-3D-DESIGN-20261021.md §3.2 and §7).
//
// The page's TimeProvider hears the primary video only from `timeupdate`,
// about four times a second, so an arm driven by it alone would jump every
// 250 ms. This module turns three sources into one stream of episode times,
// with no React in it:
//
// - playing: the primary video's `requestVideoFrameCallback`, one call per
//   presented frame, with the frame's own `mediaTime`;
// - paused, and every seek: the TimeProvider's `subscribe` (it is called
//   synchronously by every `seek`, whoever made it);
// - only where the browser has no `requestVideoFrameCallback`: an estimate
//   between two reports from the TimeProvider, wall time x playbackRate, and
//   never more than `MAX_EXTRAPOLATION_S` past the last report. Without that
//   cap a video stalled on its buffer lets the arm run ahead, and it jumps
//   back when playback resumes.
//
// Episode time is `mediaTime - segmentStart` (`videoToEpisodeTime`), the same
// subtraction the player does for a segmented (v3) video.
import { nextTimeEstimate, videoToEpisodeTime } from "./clock";

/** The estimate never runs further than this past the last `timeupdate` (seconds). */
export const MAX_EXTRAPOLATION_S = 0.5;
/**
 * While frames are arriving, a time the page reports this close to the last
 * presented frame is the two clocks drifting, not a seek: it is ignored (the
 * player itself only re-seeks the video when they differ by more than 0.2 s).
 */
export const SEEK_TOLERANCE_S = 0.25;
/** A frame callback this recent counts as "frames are arriving". */
const FRAME_FRESH_MS = 500;

/** What the clock needs of the primary `<video>`; an `HTMLVideoElement` fits. */
export interface ReplayVideo {
  paused: boolean;
  ended?: boolean;
  currentTime: number;
  playbackRate: number;
  requestVideoFrameCallback?: (
    callback: (now: number, metadata: { mediaTime: number }) => void,
  ) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
  addEventListener?: (type: string, listener: () => void) => void;
  removeEventListener?: (type: string, listener: () => void) => void;
}

/** The primary video and where its segment starts in the file (0 when not segmented). */
export interface ReplayVideoSource {
  el: ReplayVideo;
  segmentStart: number;
}

export interface ReplayClockOptions {
  /** `useTime().subscribe`: called with the episode time on every report and seek. */
  subscribe: (callback: (episodeTime: number) => void) => () => void;
  video: ReplayVideoSource | null;
  /** Receives episode-relative seconds; keep it cheap, it can run once per frame. */
  onTime: (episodeTime: number) => void;
  /** Milliseconds, monotonic. Default `performance.now`. */
  now?: () => number;
  /** Default `requestAnimationFrame` / `cancelAnimationFrame`. */
  requestFrame?: (callback: () => void) => number;
  cancelFrame?: (handle: number) => void;
  maxExtrapolation?: number;
}

/** Starts the clock; the returned function stops it and releases every handle. */
export function startReplayClock(options: ReplayClockOptions): () => void {
  const { subscribe, video, onTime } = options;
  const now = options.now ?? (() => performance.now());
  const requestFrame =
    options.requestFrame ?? ((callback) => requestAnimationFrame(callback));
  const cancelFrame =
    options.cancelFrame ?? ((handle) => cancelAnimationFrame(handle));
  const maxAhead = options.maxExtrapolation ?? MAX_EXTRAPOLATION_S;
  const el = video?.el ?? null;
  const segmentStart = video?.segmentStart ?? 0;
  const hasFrameCallback =
    el !== null && typeof el.requestVideoFrameCallback === "function";
  const playing = () => el !== null && !el.paused && !el.ended;

  let stopped = false;
  let reported: { t: number; wall: number } | null = null;
  let lastFrame: { t: number; wall: number } | null = null;
  let frameHandle: number | null = null;
  let rafHandle: number | null = null;

  const unsubscribe = subscribe((t) => {
    reported = { t, wall: now() };
    const followingFrames =
      hasFrameCallback &&
      playing() &&
      lastFrame !== null &&
      now() - lastFrame.wall <= FRAME_FRESH_MS &&
      Math.abs(t - lastFrame.t) <= SEEK_TOLERANCE_S;
    if (!followingFrames) onTime(t);
  });

  const onFrame = (_now: number, metadata?: { mediaTime?: number }) => {
    frameHandle = null;
    if (stopped || el === null) return;
    const media =
      typeof metadata?.mediaTime === "number"
        ? metadata.mediaTime
        : el.currentTime;
    const t = videoToEpisodeTime(media, segmentStart);
    lastFrame = { t, wall: now() };
    // Paused, a frame is a seek settling: the page's own time already moved
    // the arm (see `subscribe` above) and stays the reference.
    if (playing()) onTime(t);
    frameHandle = el.requestVideoFrameCallback!(onFrame);
  };
  if (hasFrameCallback) frameHandle = el.requestVideoFrameCallback!(onFrame);

  const tick = () => {
    rafHandle = null;
    if (stopped || el === null || !playing()) return;
    if (reported !== null) {
      const estimate = nextTimeEstimate(
        reported.t,
        reported.wall,
        now(),
        el.playbackRate,
      );
      onTime(Math.min(estimate, reported.t + maxAhead));
    }
    rafHandle = requestFrame(tick);
  };
  // Only a browser without frame callbacks needs the estimate, and only while
  // playing: the loop ends itself on pause and `play` starts it again.
  const startEstimate = () => {
    if (hasFrameCallback || stopped || rafHandle !== null || !playing()) return;
    rafHandle = requestFrame(tick);
  };
  if (!hasFrameCallback) {
    el?.addEventListener?.("play", startEstimate);
    el?.addEventListener?.("playing", startEstimate);
    startEstimate();
  }

  return () => {
    if (stopped) return;
    stopped = true;
    unsubscribe();
    if (frameHandle !== null) el?.cancelVideoFrameCallback?.(frameHandle);
    if (rafHandle !== null) cancelFrame(rafHandle);
    frameHandle = null;
    rafHandle = null;
    el?.removeEventListener?.("play", startEstimate);
    el?.removeEventListener?.("playing", startEstimate);
  };
}
