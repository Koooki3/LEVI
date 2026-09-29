/**
 * Live segmentation store: the panel's EventSource writes results here, each
 * camera's overlay canvas reads them from its own animation-frame loop. No
 * React state is involved per frame, so a busy stream never re-renders the
 * page or delays playback.
 */

import type { LiveResult } from "@/types/segmentation.types";

/** Results kept per camera (frame_index → result). */
export const LIVE_FRAMES_KEPT = 64;
/** A result may stand in for at most this many later frames. */
export const LIVE_STALE_FRAMES = 3;

type Listener = () => void;

interface LiveStore {
  sessionId: string | null;
  episodeIndex: number | null;
  fps: number;
  results: Map<string, Map<number, LiveResult>>;
  videos: Map<string, { el: HTMLVideoElement; segmentStart: number }>;
}

const store: LiveStore = {
  sessionId: null,
  episodeIndex: null,
  fps: 30,
  results: new Map(),
  videos: new Map(),
};
const listeners = new Set<Listener>();

function notify() {
  for (const listener of listeners) listener();
}

export function subscribeLive(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Start (or, with `sessionId = null`, end) the overlay for one episode. */
export function setLiveSession(
  sessionId: string | null,
  episodeIndex: number | null,
  fps?: number | null,
): void {
  store.sessionId = sessionId;
  store.episodeIndex = sessionId ? episodeIndex : null;
  if (fps && fps > 0) store.fps = fps;
  store.results = new Map();
  notify();
}

export function liveSession(): {
  sessionId: string | null;
  episodeIndex: number | null;
  fps: number;
} {
  return {
    sessionId: store.sessionId,
    episodeIndex: store.episodeIndex,
    fps: store.fps,
  };
}

/** Insert into a bounded frame map, evicting the results that arrived
 * first. (Evicting the lowest frame indices would throw away every new
 * result after the player loops or seeks back.) */
export function insertBounded<T>(
  frames: Map<number, T>,
  frame: number,
  value: T,
  limit = LIVE_FRAMES_KEPT,
): void {
  frames.delete(frame);
  frames.set(frame, value);
  for (const oldest of frames.keys()) {
    if (frames.size <= limit) break;
    frames.delete(oldest);
  }
}

export function pushLiveResult(result: LiveResult): void {
  if (!store.sessionId) return;
  let frames = store.results.get(result.camera_key);
  if (!frames) {
    frames = new Map();
    store.results.set(result.camera_key, frames);
  }
  insertBounded(frames, result.frame_index, result);
}

export function liveResults(
  cameraKey: string,
): Map<number, LiveResult> | undefined {
  return store.results.get(cameraKey);
}

/** Canvases register their video so the panel can follow the player clock. */
export function registerLiveVideo(
  cameraKey: string,
  el: HTMLVideoElement,
  segmentStart: number,
): () => void {
  store.videos.set(cameraKey, { el, segmentStart });
  notify();
  return () => {
    if (store.videos.get(cameraKey)?.el === el) {
      store.videos.delete(cameraKey);
      notify();
    }
  };
}

/** The first registered video still in the document (the clock source). */
export function primaryLiveVideo(): {
  el: HTMLVideoElement;
  segmentStart: number;
} | null {
  for (const entry of store.videos.values())
    if (entry.el.isConnected) return entry;
  return null;
}

/** Episode-local time each registered camera shows now. Cameras of one
 * player drift apart by up to a few frames (more at high speed), so the
 * worker follows each camera's own time. */
export function liveCameraTimes(): Record<string, number> {
  const times: Record<string, number> = {};
  for (const [camera, entry] of store.videos)
    if (entry.el.isConnected)
      times[camera] = Math.max(0, entry.el.currentTime - entry.segmentStart);
  return times;
}

/** Episode-local frame shown by a video at `currentTime` seconds. */
export function displayedFrame(
  currentTime: number,
  fps: number,
  segmentStart = 0,
): number {
  const local = Math.max(0, currentTime - segmentStart);
  return Math.floor(local * fps + 1e-6);
}

/** The result to draw for `frame`: its own, else the newest earlier result
 * at most `maxStale` frames old, else none (a late mask is never shown). */
export function chooseResult<T>(
  frames: Map<number, T> | undefined,
  frame: number,
  maxStale = LIVE_STALE_FRAMES,
): T | null {
  if (!frames) return null;
  for (let f = frame; f >= frame - maxStale; f -= 1) {
    const value = frames.get(f);
    if (value !== undefined) return value;
  }
  return null;
}

/** Decode COCO uncompressed (column-major) RLE into a row-major 0/1 mask. */
export function decodeRle(rle: { size: [number, number]; counts: number[] }): {
  width: number;
  height: number;
  data: Uint8Array;
} {
  const [height, width] = rle.size;
  const data = new Uint8Array(Math.max(0, width * height));
  let cursor = 0;
  let foreground = false;
  for (const count of rle.counts) {
    if (foreground) {
      const end = Math.min(cursor + count, width * height);
      for (let linear = cursor; linear < end; linear += 1) {
        const y = linear % height;
        const x = (linear - y) / height;
        data[y * width + x] = 1;
      }
    }
    cursor += count;
    foreground = !foreground;
  }
  return { width, height, data };
}

/** Parse "0-20, 25" into sorted unique episode indices; throws on bad input. */
export function parseEpisodeList(value: string): number[] {
  const out = new Set<number>();
  for (const raw of value.split(/[,;\n]+/)) {
    const part = raw.trim();
    if (!part) continue;
    const range = /^(\d+)\s*-\s*(\d+)$/.exec(part);
    if (range) {
      const lo = Number(range[1]);
      const hi = Number(range[2]);
      if (hi < lo) throw new Error(`Invalid range: ${part}`);
      if (hi - lo > 100_000) throw new Error(`Range too large: ${part}`);
      for (let i = lo; i <= hi; i += 1) out.add(i);
    } else if (/^\d+$/.test(part)) {
      out.add(Number(part));
    } else {
      throw new Error(`Invalid episode: ${part}`);
    }
  }
  return [...out].sort((a, b) => a - b);
}

/** Fire-and-forget sender with at most one call in flight: values sent while
 * one is pending collapse into the latest, which goes out next. */
export function createCoalescer<V>(
  send: (value: V) => Promise<unknown>,
): (value: V) => void {
  let inFlight = false;
  let pending: { value: V } | null = null;
  const pump = () => {
    if (inFlight || !pending) return;
    const { value } = pending;
    pending = null;
    inFlight = true;
    let call: Promise<unknown>;
    try {
      call = send(value);
    } catch (error) {
      call = Promise.reject(error);
    }
    call
      .catch(() => undefined)
      .finally(() => {
        inFlight = false;
        pump();
      });
  };
  return (value: V) => {
    pending = { value };
    pump();
  };
}
