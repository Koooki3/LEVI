import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import {
  canCheckFci,
  canCheckServer,
  canStartSequence,
  actView,
  compPhrase,
  markerRemaining,
  phraseFor,
  readinessProgress,
  startControl,
  stopControl,
  COMPS,
  type ActView,
} from "../stack-act-logic";
import { STACK_VIEWS, stackDoc } from "./ctl-fixtures";
import { stackView } from "../stack-status-logic";

const KNOWN = new Set(Object.keys(en));
const act = (
  name: string,
  change?: (doc: Record<string, any>) => void, // eslint-disable-line @typescript-eslint/no-explicit-any
) => actView(stackDoc(name, change), KNOWN, stackView(stackDoc(name), KNOWN))!;
const states = (view: ActView) =>
  Object.fromEntries(COMPS.map((id) => [id, view.comps[id].state]));

describe("the backend's own views, read by the page", () => {
  test("every named view of the backend reads, and none reads as unreadable", () => {
    expect(STACK_VIEWS.length).toBeGreaterThanOrEqual(15);
    for (const name of STACK_VIEWS) {
      const view = act(name);
      expect(view).not.toBeNull();
      // The page's state is the backend's word, never a re-derivation.
      const raw = stackDoc(name).act.terminals;
      for (const id of COMPS)
        expect(view.comps[id].state).toBe(raw[id.toLowerCase()].state);
    }
  });

  test("states follow the backend", () => {
    expect(states(act("idle"))).toEqual({
      M: "idle",
      A: "idle",
      B: "unknown",
      C: "idle",
    });
    expect(states(act("a_starting")).A).toBe("starting");
    expect(states(act("a_ready")).A).toBe("ready");
    expect(states(act("a_failed")).A).toBe("failed");
    expect(states(act("c_ready"))).toEqual({
      M: "ready",
      A: "ready",
      B: "ready",
      C: "ready",
    });
  });

  test("a start is offered exactly when the backend says so, with its blockers beside it when not", () => {
    const ready = act("ready_to_start_a");
    expect(startControl(ready.comps.A)).toMatchObject({
      enabled: true,
    });
    const idle = act("idle");
    const a = startControl(idle.comps.A);
    expect(a.enabled).toBe(false);
    // The backend's own codes, in the order it sent them.
    expect(a.blockers.map((b) => b.code)).toContain("gate.monitor_fresh");
    expect(a.blockers.map((b) => b.code)).toContain("gate.marker_set");
    // Worded from the catalogue, with the facts that fill the sentence.
    for (const blocker of a.blockers)
      expect(blocker.worded.say?.key ?? "").toMatch(/^marvel0\./);
  });

  test("a blocker the catalogue does not know shows the server's sentence, never a made-up one", () => {
    const view = act("idle", (doc) => {
      doc.act.terminals.a.start_blockers = [
        {
          code: "brand_new",
          level: "required",
          text: "Server words",
          params: {},
        },
      ];
    });
    expect(view.comps.A.startBlockers[0].worded).toEqual({
      say: null,
      text: "Server words",
    });
  });

  test("a sentence whose facts did not arrive is not printed with a bare placeholder", () => {
    const view = act("idle", (doc) => {
      doc.act.terminals.a.start_blockers = [
        {
          code: "gate.fci_probe",
          level: "required",
          text: "English: check FCI",
          params: {},
        },
      ];
    });
    expect(view.comps.A.startBlockers[0].worded).toEqual({
      say: null,
      text: "English: check FCI",
    });
  });

  test("the same blocker twice is listed once", () => {
    const view = act("a_ready");
    const codes = view.comps.M.stopBlockers.map((b) => b.code);
    expect(codes).toEqual([...new Set(codes)]);
  });

  test("the guard is the backend's: outside the product, or with the control file off, nothing is offered", () => {
    const fake = act("ready_to_start_a", (doc) => {
      doc.profile = "fake";
    });
    expect(fake.real).toBe(false);
    for (const id of COMPS) {
      expect(startControl(fake.comps[id]).enabled).toBe(false);
      expect(stopControl(fake.comps[id]).enabled).toBe(false);
    }
    expect(fake.marker.canTouch || fake.marker.canAdopt).toBe(false);
    expect(canCheckFci(fake) || canCheckServer(fake)).toBe(false);
    expect(canStartSequence(fake)).toBe(false);
    const off = act("control_disabled");
    expect(off.enabled).toBe(false);
    expect(startControl(off.comps.A).enabled).toBe(false);
    expect(off.comps.A.startBlockers[0].code).toBe("control_disabled");
  });

  test("a document without the controls reads as none; a terminal missing from it offers nothing", () => {
    expect(actView({ ...stackDoc("idle"), act: undefined }, KNOWN)).toBeNull();
    expect(actView(null, KNOWN)).toBeNull();
    const view = act("ready_to_start_a", (doc) => {
      delete doc.act.terminals.a;
    });
    expect(view.comps.A.state).toBe("unknown");
    expect(startControl(view.comps.A).enabled).toBe(false);
    // A value of the wrong type is false, not true.
    const strict = act("ready_to_start_a", (doc) => {
      doc.act.terminals.a.can_start = "yes";
    });
    expect(startControl(strict.comps.A).enabled).toBe(false);
  });
});

describe("starting: what has been seen and what is still missing", () => {
  test("A while it starts", () => {
    const a = act("a_starting").comps.A;
    expect(a.readiness!.seen.map((l) => l.key)).toEqual(["fifo"]);
    expect(a.readiness!.missing.map((l) => l.key)).toEqual([
      "spawners",
      "health_hardware_active",
    ]);
    expect(a.readiness!.waitedS).toBe(12);
    expect(a.readiness!.timeoutS).toBe(90);
    expect(readinessProgress(a.readiness)).toEqual({ seen: 1, total: 3 });
    // The lines are worded from the catalogue.
    expect(a.readiness!.seen[0].worded.say).toEqual({
      key: "marvel0.ctl.ready.fifo",
    });
  });

  test("no ready lines, no progress", () => {
    expect(readinessProgress(null)).toBeNull();
    expect(
      readinessProgress({ seen: [], missing: [], waitedS: 0, timeoutS: 1 }),
    ).toBeNull();
  });

  test("a failure is worded and its reason is the backend's code", () => {
    const a = act("a_failed").comps.A;
    expect(a.state).toBe("failed");
    expect(a.failure?.say?.key).toMatch(/^marvel0\.ctl\.failure\./);
  });
});

describe("stopping C with a client on it", () => {
  test("the backend says the override phrase would lift the refusal: the button stays on and asks for it", () => {
    const c = act("c_ready_with_client").comps.C;
    expect(c.canStop).toBe(false);
    expect(c.canStopWithOverride).toBe(true);
    expect(stopControl(c)).toMatchObject({
      enabled: true,
      needsOverride: true,
    });
    expect(c.override).toBe("stop robot server with client connected");
  });

  test("without a client it is a plain stop", () => {
    const c = act("c_ready").comps.C;
    expect(stopControl(c)).toMatchObject({
      enabled: true,
      needsOverride: false,
    });
  });

  test("A cannot be stopped under C, and says why", () => {
    const a = act("c_ready").comps.A;
    expect(stopControl(a).enabled).toBe(false);
    expect(a.stopBlockers.map((b) => b.code)).toEqual(["stop_c_first"]);
  });
});

describe("the marker, the checks, the gripper test and the guided start", () => {
  test("the marker counts down from the sample", () => {
    const young = act("idle", (doc) => {
      doc.act.marker = {
        state: "present",
        age_s: 20,
        ctime_age_s: 20,
        ours: true,
        wait_s: 60,
        remaining_s: 40,
      };
    }).marker;
    expect(markerRemaining(young, 0)).toBe(40);
    expect(markerRemaining(young, 15)).toBe(25);
    expect(markerRemaining(young, 100)).toBe(0);
    expect(markerRemaining(act("idle").marker, 5)).toBeNull();
  });

  test("a marker nobody of the page made can be adopted; none at all can be set", () => {
    const foreign = act("idle", (doc) => {
      doc.act.marker = {
        state: "present",
        age_s: 9,
        ours: false,
        wait_s: 60,
        remaining_s: 51,
      };
    }).marker;
    expect(foreign).toMatchObject({
      canAdopt: true,
      canTouch: false,
      canRelease: false,
    });
    expect(act("idle").marker).toMatchObject({
      canTouch: true,
      canAdopt: false,
    });
    // Release is the passive card's (only when the arm shows no control
    // connection and A is gone), never offered on the marker alone.
    expect(act("a_ready").marker.canRelease).toBe(false);
  });

  test("the FCI check is offered before A, never while A runs; the checks before C need A and B", () => {
    expect(canCheckFci(act("idle"))).toBe(true);
    expect(canCheckFci(act("a_ready"))).toBe(false);
    expect(canCheckServer(act("a_ready"))).toBe(false);
    expect(canCheckServer(act("b_ready"))).toBe(true);
    expect(act("a_ready").fci).toMatchObject({ state: "open", fresh: true });
    expect(act("c_ready").server).toMatchObject({
      ok: true,
      qos: true,
      controllers: true,
      eeFrame: true,
    });
    expect(act("idle").fci).toBeNull();
  });

  test("while a backend operation runs, nothing else is offered", () => {
    const view = act("b_ready", (doc) => {
      doc.act.operation = {
        kind: "gripper",
        terminal: "b",
        state: "running",
        started_at: 1,
        finished_at: null,
        code: null,
      };
    });
    expect(
      canCheckFci(view) || canCheckServer(view) || canStartSequence(view),
    ).toBe(false);
  });

  test("the gripper test's last result and its blockers", () => {
    const ok = act("gripper_success").gripper;
    expect(ok.canRun).toBe(true);
    expect(ok.last).toMatchObject({
      action: "close",
      ok: true,
      reached: true,
      stalled: false,
      code: "reached",
    });
    const bad = act("gripper_failed").gripper;
    expect(bad.last).toMatchObject({
      ok: false,
      stalled: true,
      code: "stalled",
    });
    const off = act("idle").gripper;
    expect(off.canRun).toBe(false);
    expect(off.blockers.map((b) => b.code)).toContain("gripper_b_not_ready");
  });

  test("the guided start: running, waiting for the person before B, and when it may be started", () => {
    expect(act("sequence_running").sequence).toMatchObject({
      state: "running",
      step: "monitor",
    });
    expect(act("sequence_waiting_b").sequence).toMatchObject({
      state: "waiting_confirm",
      next: "B",
    });
    expect(canStartSequence(act("idle"))).toBe(true);
    expect(canStartSequence(act("sequence_running"))).toBe(false);
    expect(canStartSequence(act("a_ready"))).toBe(false);
  });
});

describe("the phrases", () => {
  test("a terminal's phrase is the backend's, else the design record's", () => {
    const view = act("idle");
    expect(compPhrase(view, "start", "A")).toBe("start arm stack");
    expect(compPhrase(view, "stop", "C")).toBe("stop robot server");
    expect(phraseFor(view, "gripper_open")).toBe("gripper open");
    expect(phraseFor(null, "gripper_close")).toBe("gripper close");
    expect(compPhrase(null, "start", "B")).toBe("start gripper stack");
    expect(compPhrase(null, "stop", "M")).toBe("stop health monitor");
    const changed = act("idle", (doc) => {
      doc.act.terminals.a.confirm = { start: "begin A", stop: "end A" };
    });
    expect(compPhrase(changed, "start", "A")).toBe("begin A");
  });
});
