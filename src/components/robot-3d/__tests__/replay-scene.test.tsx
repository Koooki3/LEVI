import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act, forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import { TimeProvider, useTime } from "@/context/time-context";
import { FR3_JOINT_LIMITS } from "@/lib/robot-studio/fr3-kinematics";
import { detectFr3Replay } from "@/lib/replay/fr3-columns";
import { buildReplayData, type ReplayData } from "@/lib/replay/frames";

// Canvas is a plain div here: no WebGL. The arm is a recorder that remembers
// every pose it is given and which instance (solid or ghost) got it.
type Call = { ghost: boolean; joints: number[] };
let calls: Call[] = [];
let failLoad = false;
// A model that loads when the test says so (and ignores poses until then, as the real one does).
let holdLoads = false;
let loadGates: Array<() => void> = [];
const invalidate = mock(() => {});
mock.module("@react-three/fiber", () => ({
  Canvas: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="canvas">{children}</div>
  ),
  useThree: () => ({ invalidate }),
}));
mock.module("@react-three/drei", () => ({ OrbitControls: () => null }));
mock.module("../fr3-model", () => ({
  FR3_CANVAS_PROPS: {},
  FR3_ORBIT_TARGET: [0, 0, 0],
  Fr3Backdrop: () => null,
  Fr3CameraView: () => null,
  Fr3Robot: forwardRef<
    { setJoints: (q: ArrayLike<number>) => void },
    { ghost?: boolean; onLoad: (ok: boolean) => void }
  >(function FakeRobot({ ghost = false, onLoad }, ref) {
    const loaded = useRef(false);
    useImperativeHandle(ref, () => ({
      setJoints: (q) => {
        if (loaded.current) calls.push({ ghost, joints: Array.from(q) });
      },
    }));
    useEffect(() => {
      const done = () => {
        loaded.current = true;
        onLoad(!failLoad);
      };
      if (holdLoads) loadGates.push(done);
      else done();
    }, [onLoad]);
    return null;
  }),
}));
const { default: ReplayScene } = await import("../replay-scene");
setupDom();

const consoleError = console.error;
beforeEach(() => {
  calls = [];
  failLoad = false;
  holdLoads = false;
  loadGates = [];
  invalidate.mockClear();
  console.error = () => {}; // <primitive> is a three element
});
afterEach(() => {
  console.error = consoleError;
});

const names = Array.from({ length: 7 }, (_, i) => `joint_${i + 1}`);
const mid = (c: number) =>
  (FR3_JOINT_LIMITS[c].lower + FR3_JOINT_LIMITS[c].upper) / 2;
const stateJoint = (i: number, c: number) => mid(c) + i * 0.01;
const actionJoint = (i: number, c: number) => mid(c) - i * 0.01;
/** 40 rows at 0.1 s: state rises, action falls, so the two are never confused. */
function data(): ReplayData {
  const rows = Array.from({ length: 40 }, (_, i) => {
    const row: Record<string, number> = { timestamp: i * 0.125 };
    names.forEach((name, c) => {
      row[`observation.state | ${name}`] = stateJoint(i, c);
      row[`action | ${name}`] = actionJoint(i, c);
    });
    return row;
  });
  const flat = [
    "timestamp",
    ...names.map((n) => `observation.state | ${n}`),
    ...names.map((n) => `action | ${n}`),
  ];
  return buildReplayData(detectFr3Replay({ robot_type: "fr3" }, flat), rows)!;
}

/** One TimeProvider, one primary <video> with requestVideoFrameCallback. */
function makeClockSources() {
  const listeners = new Set<(t: number) => void>();
  const video = Object.assign(new EventTarget(), {
    paused: true,
    ended: false,
    currentTime: 0,
    playbackRate: 1,
    frameCallbacks: new Map<
      number,
      (n: number, m: { mediaTime: number }) => void
    >(),
    next: 1,
    requestVideoFrameCallback(
      cb: (n: number, m: { mediaTime: number }) => void,
    ) {
      const handle = this.next++;
      this.frameCallbacks.set(handle, cb);
      return handle;
    },
    cancelVideoFrameCallback(handle: number) {
      this.frameCallbacks.delete(handle);
    },
    present(mediaTime: number) {
      const due = [...this.frameCallbacks.values()];
      this.frameCallbacks.clear();
      due.forEach((cb) => cb(0, { mediaTime }));
    },
  });
  return {
    video,
    listeners,
    subscribe: (cb: (t: number) => void) => {
      listeners.add(cb);
      return () => void listeners.delete(cb);
    },
    seek: (t: number) => listeners.forEach((cb) => cb(t)),
  };
}

type Sources = ReturnType<typeof makeClockSources>;
function scene(sources: Sources, replay: ReplayData, extra = {}) {
  const videoSource = { el: sources.video, segmentStart: 0 };
  return (
    <ReplayScene
      data={replay}
      showAction={false}
      trail={false}
      view="perspective"
      viewRevision={0}
      subscribe={sources.subscribe}
      video={videoSource}
      initialTime={0}
      onLoad={() => {}}
      {...extra}
    />
  );
}
const solidCalls = () => calls.filter((call) => !call.ghost);
const expectRow = (joints: number[], row: number, joint = stateJoint) =>
  joints.forEach((value, c) => expect(value).toBeCloseTo(joint(row, c), 12));

describe("ReplayScene: the pose handed to the model", () => {
  test("a seek gives the joints of the row at that time", async () => {
    const sources = makeClockSources();
    await render(scene(sources, data()));
    await act(async () => sources.seek(2 * 0.125));
    expectRow(solidCalls().at(-1)!.joints, 2);
    await act(async () => sources.seek(30 * 0.125));
    expectRow(solidCalls().at(-1)!.joints, 30);
    // Between two rows: the average of both.
    await act(async () => sources.seek(10.5 * 0.125));
    solidCalls()
      .at(-1)!
      .joints.forEach((value, c) =>
        expect(value).toBeCloseTo(
          (stateJoint(10, c) + stateJoint(11, c)) / 2,
          12,
        ),
      );
  });

  test("before any report the arm is at initialTime, not at row 0", async () => {
    const sources = makeClockSources();
    await render(scene(sources, data(), { initialTime: 7 * 0.125 }));
    expectRow(solidCalls().at(-1)!.joints, 7);
  });

  test("a model that finishes loading after a seek is posed at that seek, not at the start", async () => {
    const sources = makeClockSources();
    holdLoads = true;
    await render(scene(sources, data()));
    await act(async () => sources.seek(9 * 0.125));
    expect(calls).toEqual([]); // not loaded: the poses were dropped
    await act(async () => loadGates.splice(0).forEach((done) => done()));
    expectRow(solidCalls().at(-1)!.joints, 9);
  });

  test("the model keeps what it is given after the last report (a pause)", async () => {
    const sources = makeClockSources();
    await render(scene(sources, data()));
    await act(async () => sources.seek(5 * 0.125));
    const count = calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    expect(calls.length).toBe(count);
  });

  test("playing: about one update per video frame (30 in a second), not four", async () => {
    const sources = makeClockSources();
    await render(scene(sources, data()));
    const before = solidCalls().length;
    sources.video.paused = false;
    await act(async () => {
      for (let frame = 1; frame <= 30; frame++) {
        sources.video.present(frame / 30);
        if (frame % 8 === 0) sources.seek(frame / 30); // timeupdate, ~4 a second
      }
    });
    const updates = solidCalls().length - before;
    expect(updates).toBeGreaterThanOrEqual(28);
    expect(updates).toBeLessThanOrEqual(33);
    // The arm ends where the video is: 1 s, between rows 7 (0.875) and 8 (1.0).
    expectRow(solidCalls().at(-1)!.joints, 8);
  });

  test("a segmented video drives the arm with episode time", async () => {
    const sources = makeClockSources();
    const replay = data();
    const el = sources.video;
    await render(
      <ReplayScene
        data={replay}
        showAction={false}
        trail={false}
        view="top"
        viewRevision={0}
        subscribe={sources.subscribe}
        video={{ el, segmentStart: 100 }}
        initialTime={0}
        onLoad={() => {}}
      />,
    );
    el.paused = false;
    await act(async () => el.present(100 + 4 * 0.125));
    expectRow(solidCalls().at(-1)!.joints, 4);
  });

  test("the clock stops with the scene: no listener and no frame callback is left", async () => {
    const sources = makeClockSources();
    const rendered = await render(scene(sources, data()));
    expect(sources.listeners.size).toBe(1);
    expect(sources.video.frameCallbacks.size).toBe(1);
    await rendered.rerender(<div />);
    expect(sources.listeners.size).toBe(0);
    expect(sources.video.frameCallbacks.size).toBe(0);
  });
});

describe("ReplayScene: the action ghost and loading", () => {
  test("the ghost arm exists only with showAction, and is posed with the action row", async () => {
    const sources = makeClockSources();
    const replay = data();
    const rendered = await render(scene(sources, replay));
    await act(async () => sources.seek(6 * 0.125));
    expect(calls.some((call) => call.ghost)).toBe(false);
    await rendered.rerender(scene(sources, replay, { showAction: true }));
    const ghostCall = calls.filter((call) => call.ghost).at(-1)!;
    expectRow(ghostCall.joints, 6, actionJoint);
    expectRow(solidCalls().at(-1)!.joints, 6);
  });

  test("a new onLoad prop does not make the model load again", async () => {
    const sources = makeClockSources();
    const replay = data();
    holdLoads = true;
    const rendered = await render(scene(sources, replay, { onLoad: () => {} }));
    const mounts = loadGates.length;
    await rendered.rerender(scene(sources, replay, { onLoad: () => {} }));
    expect(loadGates.length).toBe(mounts);
  });

  test("a model that fails to load is reported, and a pose-mode scene needs none", async () => {
    const sources = makeClockSources();
    const onLoad = mock((ok: boolean) => void ok);
    failLoad = true;
    await render(scene(sources, data(), { onLoad }));
    expect(onLoad).toHaveBeenCalledWith(false);

    const names6 = ["x", "y", "z", "rx", "ry", "rz"];
    const rows = Array.from({ length: 5 }, (_, i) => ({
      timestamp: i,
      ...Object.fromEntries(names6.map((n) => [`observation.state | ${n}`, i])),
    }));
    const poseData = buildReplayData(
      detectFr3Replay({ robot_type: "generic_arm" }, [
        "timestamp",
        ...names6.map((n) => `observation.state | ${n}`),
      ]),
      rows,
    )!;
    calls = [];
    const poseLoaded = mock((ok: boolean) => void ok);
    await render(scene(sources, poseData, { onLoad: poseLoaded }));
    expect(poseLoaded).toHaveBeenCalledWith(true);
    expect(calls).toEqual([]);
  });
});

describe("ReplayScene: a hole in the first row, under the page's own TimeProvider", () => {
  type Time = ReturnType<typeof useTime>;
  let time: Time;
  function Bound({ replay }: { replay: ReplayData }) {
    time = useTime();
    return (
      <>
        <output>{time.currentTime.toFixed(3)}</output>
        <ReplayScene
          data={replay}
          showAction={false}
          trail={false}
          view="perspective"
          viewRevision={0}
          subscribe={time.subscribe}
          video={null}
          initialTime={0}
          onLoad={() => {}}
        />
      </>
    );
  }

  test("the arm starts at the first whole row and every seek still moves the arm and the page's time", async () => {
    const replay = data();
    (replay.state.values as Float64Array)[4] = NaN; // row 0, joint 5
    const { host } = await render(
      <TimeProvider duration={10}>
        <Bound replay={replay} />
      </TimeProvider>,
    );
    // Posed at t = 0 over the hole: the first whole row (row 1), inside the limits.
    expectRow(solidCalls().at(-1)!.joints, 1);
    await act(async () => time.seek(3 * 0.125));
    expectRow(solidCalls().at(-1)!.joints, 3);
    expect(host.querySelector("output")!.textContent).toBe("0.375");
    // Back over the hole: the arm holds the pose it had.
    await act(async () => time.seek(0));
    expectRow(solidCalls().at(-1)!.joints, 3);
    await act(async () => time.seek(6 * 0.125));
    expectRow(solidCalls().at(-1)!.joints, 6);
    expect(host.querySelector("output")!.textContent).toBe("0.750");
  });
});
