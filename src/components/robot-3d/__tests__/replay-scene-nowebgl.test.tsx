// The real <Canvas> (React Three Fiber) in a DOM without WebGL: happy-dom's
// canvas.getContext() returns null, which is what a browser with WebGL turned
// off answers. Before the guard the renderer's "Error creating WebGL context."
// was an unhandled promise rejection and the scene never reported anything.
import { render, setupDom } from "@/components/ds/__tests__/dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  describe,
  expect,
  test,
} from "bun:test";
import { act } from "react";
import { TimeProvider } from "@/context/time-context";
import { FR3_JOINT_LIMITS } from "@/lib/robot-studio/fr3-kinematics";
import { detectFr3Replay } from "@/lib/replay/fr3-columns";
import { buildReplayData } from "@/lib/replay/frames";
import ReplayScene from "../replay-scene";

setupDom();

// react-use-measure gives the Canvas a size only when the container has one.
// (The DOM is removed after the file, so nothing needs restoring.)
beforeAll(() => {
  HTMLElement.prototype.getBoundingClientRect = function () {
    return new DOMRect(0, 0, 400, 300);
  };
});

const names = Array.from({ length: 7 }, (_, i) => `joint_${i + 1}`);
const detection = detectFr3Replay({ robot_type: "franka_panda" }, [
  "timestamp",
  ...names.map((name) => `observation.state | ${name}`),
]);
const rows = Array.from({ length: 4 }, (_, i) => {
  const row: Record<string, number> = { timestamp: i * 0.5 };
  names.forEach((name, c) => {
    row[`observation.state | ${name}`] =
      (FR3_JOINT_LIMITS[c].lower + FR3_JOINT_LIMITS[c].upper) / 2;
  });
  return row;
});
const data = buildReplayData(detection, rows)!;
const poseData = buildReplayData(
  detectFr3Replay({ robot_type: "generic_arm" }, [
    "timestamp",
    ...["x", "y", "z", "rx", "ry", "rz"].map((n) => `observation.state | ${n}`),
  ]),
  Array.from({ length: 4 }, (_, i) => ({
    timestamp: i * 0.5,
    "observation.state | x": 0.3,
    "observation.state | y": 0,
    "observation.state | z": 0.4,
    "observation.state | rx": 3,
    "observation.state | ry": 0.1,
    "observation.state | rz": 0,
  })),
)!;

const rejections: unknown[] = [];
const onRejection = (reason: unknown) => rejections.push(reason);
process.on("unhandledRejection", onRejection);
afterAll(() => process.off("unhandledRejection", onRejection));
const consoleWarn = console.warn;
const consoleError = console.error;
afterEach(() => {
  console.warn = consoleWarn;
  console.error = consoleError;
});

const logged = { warn: [] as unknown[][], error: [] as unknown[][] };
async function mount(scene: typeof data, calls: boolean[]) {
  logged.warn = [];
  logged.error = [];
  console.warn = (...args: unknown[]) => void logged.warn.push(args);
  console.error = (...args: unknown[]) => void logged.error.push(args);
  await render(
    <TimeProvider duration={2}>
      <ReplayScene
        data={scene}
        showAction={false}
        trail={false}
        view="perspective"
        viewRevision={0}
        subscribe={() => () => {}}
        video={null}
        initialTime={0}
        onLoad={(ok) => calls.push(ok)}
      />
    </TimeProvider>,
  );
  // The Canvas measures its container on a resize, then runs its async
  // configure after every render; the resizes below re-render it meanwhile.
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      window.dispatchEvent(new Event("resize"));
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
  }
}

describe("ReplayScene with no WebGL (the real Canvas)", () => {
  test("joints mode: reports failure once, with no unhandled rejection", async () => {
    const calls: boolean[] = [];
    await mount(data, calls);
    expect(calls).toEqual([false]);
    expect(rejections).toEqual([]);
    // No log flood: our one line, and the renderer's own for the one attempt.
    expect(logged.warn).toHaveLength(1);
    expect(logged.error).toHaveLength(1);
  });

  test("pose mode (which waits for no model) reports failure too", async () => {
    const calls: boolean[] = [];
    await mount(poseData, calls);
    expect(calls).toEqual([false]);
    expect(rejections).toEqual([]);
  });
});
