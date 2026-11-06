import { describe, expect, spyOn, test } from "bun:test";
import {
  MAX_EXTRAPOLATION_S,
  SEEK_TOLERANCE_S,
  startReplayClock,
  type ReplayVideo,
} from "../playback-clock";

/** A virtual page: one wall clock, one animation-frame queue, one TimeProvider. */
function makeEnv() {
  let wall = 0;
  let nextHandle = 1;
  const raf = new Map<number, () => void>();
  const listeners = new Set<(t: number) => void>();
  return {
    now: () => wall,
    requestFrame: (callback: () => void) => {
      const handle = nextHandle++;
      raf.set(handle, callback);
      return handle;
    },
    cancelFrame: (handle: number) => void raf.delete(handle),
    pendingFrames: () => raf.size,
    subscribe: (callback: (t: number) => void) => {
      listeners.add(callback);
      return () => void listeners.delete(callback);
    },
    listenerCount: () => listeners.size,
    /** What the player's `timeupdate` handler does: seek(t, "video"), or a user seek. */
    report: (t: number) => listeners.forEach((callback) => callback(t)),
    /** Advance the wall clock and run the animation frames queued before it. */
    advance(ms: number) {
      wall += ms;
      const due = [...raf.entries()];
      raf.clear();
      due.forEach(([, callback]) => callback());
    },
  };
}
type Env = ReturnType<typeof makeEnv>;

/** A <video> with requestVideoFrameCallback. `present` is one presented frame. */
class FakeVideo extends EventTarget {
  paused = true;
  ended = false;
  currentTime = 0;
  playbackRate = 1;
  private nextHandle = 1;
  private callbacks = new Map<
    number,
    (now: number, metadata: { mediaTime: number }) => void
  >();
  requestVideoFrameCallback = (
    callback: (now: number, metadata: { mediaTime: number }) => void,
  ) => {
    const handle = this.nextHandle++;
    this.callbacks.set(handle, callback);
    return handle;
  };
  cancelVideoFrameCallback = (handle: number) =>
    void this.callbacks.delete(handle);
  pendingFrameCallbacks = () => this.callbacks.size;
  present(mediaTime: number) {
    this.currentTime = mediaTime;
    const due = [...this.callbacks.values()];
    this.callbacks.clear();
    due.forEach((callback) => callback(0, { mediaTime }));
  }
  play() {
    this.paused = false;
    this.dispatchEvent(new Event("play"));
  }
  pause() {
    this.paused = true;
    this.dispatchEvent(new Event("pause"));
  }
}
/** The same without requestVideoFrameCallback (an old browser). */
class PlainVideo extends EventTarget {
  paused = true;
  ended = false;
  currentTime = 0;
  playbackRate = 1;
  play() {
    this.paused = false;
    this.dispatchEvent(new Event("play"));
  }
  pause() {
    this.paused = true;
    this.dispatchEvent(new Event("pause"));
  }
}

function start(env: Env, video: ReplayVideo | null, segmentStart = 0) {
  const times: number[] = [];
  const stop = startReplayClock({
    subscribe: env.subscribe,
    video: video && { el: video, segmentStart },
    onTime: (t) => times.push(t),
    now: env.now,
    requestFrame: env.requestFrame,
    cancelFrame: env.cancelFrame,
  });
  return { times, stop };
}

/** `start`, but `onTime` is the given function and `onError` is recorded. */
function startWith(
  env: Env,
  video: ReplayVideo | null,
  onTime: (t: number) => void,
  onError?: (error: unknown) => void,
) {
  return startReplayClock({
    subscribe: env.subscribe,
    video: video && { el: video, segmentStart: 0 },
    onTime,
    onError,
    now: env.now,
    requestFrame: env.requestFrame,
    cancelFrame: env.cancelFrame,
  });
}

/** One second of 30 fps playback: a frame every 1/30 s, a timeupdate every 250 ms. */
function playOneSecond(env: Env, video: FakeVideo, from = 0, segmentStart = 0) {
  video.play();
  for (let frame = 1; frame <= 30; frame++) {
    env.advance(1000 / 30);
    video.present(segmentStart + from + frame / 30);
    if (frame % 8 === 0) env.report(from + frame / 30);
  }
}

describe("playing with requestVideoFrameCallback", () => {
  test("updates once per video frame (about 30 in a second), not once per timeupdate (4)", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times } = start(env, video);
    playOneSecond(env, video);
    expect(times.length).toBeGreaterThanOrEqual(28);
    expect(times.length).toBeLessThanOrEqual(33);
    // The times are the frames' own, in order, ending at 1 s.
    expect(times.every((t, i) => i === 0 || t >= times[i - 1])).toBe(true);
    expect(times.at(-1)).toBeCloseTo(1, 9);
  });

  test("a segmented video reports episode time: mediaTime minus the segment start", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times } = start(env, video, 12.5);
    playOneSecond(env, video, 0, 12.5);
    expect(times[0]).toBeCloseTo(1 / 30, 9);
    expect(times.at(-1)).toBeCloseTo(1, 9);
  });

  test("a timeupdate that agrees with the frames adds no update of its own", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times } = start(env, video);
    video.play();
    env.advance(33);
    video.present(0.4);
    const before = times.length;
    env.report(0.4 - 0.1); // drift, below SEEK_TOLERANCE_S
    expect(times.length).toBe(before);
  });

  test("a time far from the frames while playing is a seek and moves the arm at once", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times } = start(env, video);
    video.play();
    env.advance(33);
    video.present(0.4);
    env.report(0.4 + SEEK_TOLERANCE_S + 0.5);
    expect(times.at(-1)).toBeCloseTo(1.15, 9);
  });

  test("paused: every reported time is used, and frames presented after a seek are not", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times } = start(env, video);
    env.report(2.5);
    env.report(7.25);
    expect(times).toEqual([2.5, 7.25]);
    video.present(7.2); // the video settling on the sought frame
    video.present(7.3);
    expect(times).toEqual([2.5, 7.25]);
  });

  test("playing again after a pause follows the frames", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times } = start(env, video);
    env.report(3);
    video.play();
    video.present(3.033);
    expect(times).toEqual([3, 3.033]);
  });

  test("no video element: every reported time is used", () => {
    const env = makeEnv();
    const { times } = start(env, null);
    env.report(1);
    env.report(1.1);
    expect(times).toEqual([1, 1.1]);
    expect(env.pendingFrames()).toBe(0);
  });

  test("stop unsubscribes and cancels the pending frame callback", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times, stop } = start(env, video);
    expect(video.pendingFrameCallbacks()).toBe(1);
    stop();
    expect(video.pendingFrameCallbacks()).toBe(0);
    expect(env.listenerCount()).toBe(0);
    video.play();
    video.present(1);
    env.report(2);
    expect(times).toEqual([]);
    stop(); // twice is harmless
  });
});

describe("without requestVideoFrameCallback", () => {
  test("estimates between timeupdates, once per animation frame, and corrects on each report", () => {
    const env = makeEnv();
    const video = new PlainVideo();
    const { times } = start(env, video);
    env.report(0);
    video.play();
    // 15 animation frames of 1000/60 ms = 250 ms, then the next timeupdate.
    for (let i = 0; i < 15; i++) env.advance(1000 / 60);
    expect(times.length).toBe(1 + 15);
    expect(times.at(-1)).toBeCloseTo(0.25, 6);
    env.report(0.26); // the page's own time wins over the estimate
    expect(times.at(-1)).toBe(0.26);
    env.advance(1000 / 60);
    expect(times.at(-1)).toBeCloseTo(0.26 + 1 / 60, 6);
  });

  test("the estimate follows playbackRate", () => {
    const env = makeEnv();
    const video = new PlainVideo();
    video.playbackRate = 2;
    const { times } = start(env, video);
    env.report(1);
    video.play();
    env.advance(100);
    expect(times.at(-1)).toBeCloseTo(1.2, 9);
  });

  test("a stalled video: the estimate stops MAX_EXTRAPOLATION_S after the last report", () => {
    const env = makeEnv();
    const video = new PlainVideo();
    const { times } = start(env, video);
    env.report(2);
    video.play();
    for (let i = 0; i < 120; i++) env.advance(1000 / 60); // two seconds, no timeupdate
    expect(Math.max(...times)).toBeCloseTo(2 + MAX_EXTRAPOLATION_S, 9);
    expect(times.at(-1)).toBeCloseTo(2 + MAX_EXTRAPOLATION_S, 9);
    // Playback resumes at 2.2: the arm goes back at most MAX_EXTRAPOLATION_S.
    env.report(2.2);
    expect(times.at(-1)).toBe(2.2);
  });

  test("pausing ends the animation-frame loop and play starts it again", () => {
    const env = makeEnv();
    const video = new PlainVideo();
    start(env, video);
    env.report(1);
    video.play();
    expect(env.pendingFrames()).toBe(1);
    video.pause();
    env.advance(16); // the queued frame sees the pause and does not re-queue
    expect(env.pendingFrames()).toBe(0);
    video.play();
    expect(env.pendingFrames()).toBe(1);
  });

  test("stop cancels the animation-frame loop", () => {
    const env = makeEnv();
    const video = new PlainVideo();
    const { stop, times } = start(env, video);
    env.report(1);
    video.play();
    stop();
    expect(env.pendingFrames()).toBe(0);
    video.play();
    expect(env.pendingFrames()).toBe(0);
    expect(times).toEqual([1]);
  });
});

describe("an onTime that throws", () => {
  const boom = new Error("pose out of range");

  test("a seek or a report: the throw stays inside the clock, the other listeners still run", () => {
    const env = makeEnv();
    const errors: unknown[] = [];
    const seen: number[] = [];
    startWith(
      env,
      null,
      () => {
        throw boom;
      },
      (error) => errors.push(error),
    );
    // What the TimeProvider does: every listener, in turn, and then its own state.
    env.subscribe((t) => seen.push(t));
    expect(() => env.report(2.5)).not.toThrow();
    expect(seen).toEqual([2.5]);
    expect(errors).toEqual([boom]);
    // The clock is still subscribed: the next report reaches onTime again.
    env.report(3);
    expect(errors).toEqual([boom, boom]);
    expect(seen).toEqual([2.5, 3]);
  });

  test("a video frame: the next frame callback is still registered and the next frame still arrives", () => {
    const env = makeEnv();
    const video = new FakeVideo();
    const calls: number[] = [];
    const errors: unknown[] = [];
    startWith(
      env,
      video,
      (t) => {
        calls.push(t);
        if (calls.length === 1) throw boom;
      },
      (error) => errors.push(error),
    );
    video.play();
    env.advance(33);
    expect(() => video.present(0.1)).not.toThrow();
    expect(errors).toEqual([boom]);
    expect(video.pendingFrameCallbacks()).toBe(1);
    video.present(0.2);
    expect(calls).toEqual([0.1, 0.2]);
    expect(video.pendingFrameCallbacks()).toBe(1);
  });

  test("the estimate loop (no frame callbacks) keeps running after a throw", () => {
    const env = makeEnv();
    const video = new PlainVideo();
    const calls: number[] = [];
    const errors: unknown[] = [];
    startWith(
      env,
      video,
      (t) => {
        calls.push(t);
        if (calls.length === 2) throw boom;
      },
      (error) => errors.push(error),
    );
    env.report(1);
    video.play();
    for (let i = 0; i < 5; i++) env.advance(1000 / 60);
    expect(errors).toEqual([boom]);
    expect(calls.length).toBe(1 + 5);
    expect(env.pendingFrames()).toBe(1);
  });

  test("by default the first failure goes to console.error and the rest are dropped", () => {
    const env = makeEnv();
    const logged = spyOn(console, "error").mockImplementation(() => {});
    try {
      startWith(env, null, () => {
        throw boom;
      });
      env.report(1);
      env.report(2);
      env.report(3);
      expect(logged).toHaveBeenCalledTimes(1);
      expect(logged.mock.calls[0]).toContain(boom);
    } finally {
      logged.mockRestore();
    }
  });
});

describe("frames that stopped arriving", () => {
  /** Playing, one frame presented at 0.4 s of the video, no more frames after it. */
  function stalled() {
    const env = makeEnv();
    const video = new FakeVideo();
    const { times } = start(env, video);
    video.play();
    env.advance(33);
    video.present(0.4);
    return { env, video, times };
  }

  test("a report within the tolerance is ignored while the last frame is at most 500 ms old", () => {
    const { env, times } = stalled();
    const before = times.length;
    env.advance(500); // the boundary itself still counts as fresh
    env.report(0.45);
    expect(times.length).toBe(before);
  });

  test("a report within the tolerance is used once the last frame is older than 500 ms", () => {
    const { env, times } = stalled();
    const before = times.length;
    env.advance(501); // the video is playing but no frame has come: the page's time is all there is
    env.report(0.45);
    expect(times.length).toBe(before + 1);
    expect(times.at(-1)).toBe(0.45);
  });

  test("a frame arriving again makes the page's agreeing reports redundant again", () => {
    const { env, video, times } = stalled();
    env.advance(600);
    env.report(0.45); // used: frames are stale
    env.advance(33);
    video.present(0.5);
    const before = times.length;
    env.report(0.55);
    expect(times.length).toBe(before);
  });
});
