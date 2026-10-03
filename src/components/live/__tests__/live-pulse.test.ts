import { describe, expect, test } from "bun:test";
import { blockedRunsSummary, livePulse, PULSE_NOTES } from "../live-logic";
import {
  PULSE_POLL_MS,
  PulseStore,
  pulseDelay,
  type PulseDeps,
} from "../live-pulse-store";
import type { DatasetRow, LiveStatusResponse, ServiceStatus } from "../types";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const NOW = 1_800_000_000;
const dataset = (over: Partial<DatasetRow> = {}): DatasetRow => ({
  episodes: 4,
  pending: 0,
  annotating: 0,
  done: 4,
  failed: 0,
  ...over,
});
const live = (
  service: Partial<ServiceStatus> = {},
  over: Partial<LiveStatusResponse> = {},
): LiveStatusResponse => ({
  enabled: true,
  alive: true,
  service: { state: "idle", datasets: {}, sessions: [], ...service },
  ...over,
});

describe("the light of the live entry", () => {
  test("green when idle and well", () => {
    expect(livePulse(live(), 0, NOW)).toEqual({
      light: "green",
      reason: "idle",
      count: 0,
    });
  });
  test("blue while labelling, also when work waits for the model", () => {
    expect(livePulse(live({ state: "annotating" }), 0, NOW).light).toBe("blue");
    expect(livePulse(live({ state: "gpu_wait" }), 0, NOW).light).toBe("blue");
    expect(
      livePulse(
        live({ datasets: { a__x: dataset({ annotating: 2 }) } }),
        0,
        NOW,
      ).light,
    ).toBe("blue");
  });
  test("amber when labelling is paused, with the number of things to do", () => {
    const pulse = livePulse(
      live({ labelling_paused: { code: "lock", reason: "held" } }),
      0,
      NOW,
    );
    expect(pulse).toEqual({ light: "amber", reason: "paused", count: 1 });
  });
  test("amber when a plan or draft waits for a person", () => {
    const pulse = livePulse(
      live({
        datasets: {
          a__x: dataset({ state: "awaiting_approval", awaiting: "plan" }),
          b__y: dataset({ state: "awaiting_approval", awaiting: "changes" }),
        },
      }),
      0,
      NOW,
    );
    expect(pulse).toEqual({ light: "amber", reason: "awaiting", count: 2 });
  });
  test("amber only for a blocked run that needs a person; one that goes on by itself is not counted", () => {
    const waiting = livePulse(
      live(
        {},
        { blocked_runs: { count: 1, waiting: ["r1"], needs_person: [] } },
      ),
      0,
      NOW,
    );
    expect(waiting).toEqual({ light: "green", reason: "idle", count: 0 });
    const person = livePulse(
      live(
        {},
        { blocked_runs: { count: 2, waiting: ["r1"], needs_person: ["r2"] } },
      ),
      0,
      NOW,
    );
    expect(person).toEqual({
      light: "amber",
      reason: "blocked_person",
      count: 1,
    });
  });
  test("a run that goes on by itself does not hide that labelling is going on", () => {
    const pulse = livePulse(
      live(
        { state: "annotating" },
        { blocked_runs: { count: 1, waiting: ["r1"], needs_person: [] } },
      ),
      0,
      NOW,
    );
    expect(pulse.light).toBe("blue");
  });
  test("what the live page says about blocked runs", () => {
    expect(blockedRunsSummary(live())).toEqual({ waiting: 0, needsPerson: 0 });
    expect(blockedRunsSummary(null)).toEqual({ waiting: 0, needsPerson: 0 });
    expect(
      blockedRunsSummary(
        live(
          {},
          {
            blocked_runs: {
              count: 3,
              waiting: ["a"],
              needs_person: ["b", "c"],
            },
          },
        ),
      ),
    ).toEqual({ waiting: 1, needsPerson: 2 });
  });
  test("amber when the service itself is not running", () => {
    expect(livePulse(live({}, { alive: false }), 0, NOW)).toEqual({
      light: "amber",
      reason: "offline",
      count: 1,
    });
  });
  test("red for the FR3 red light or a faulted session, ahead of everything", () => {
    const red = livePulse(
      live({
        fr3: { state: "red", reasons: ["joint reflex"] },
        labelling_paused: { code: "lock" },
        state: "annotating",
      }),
      0,
      NOW,
    );
    expect(red.light).toBe("red");
    expect(red.reason).toBe("fault");
    expect(red.count).toBe(2); // the light and the pause
    const session = livePulse(
      live({
        sessions: [
          { group: "g", task_folder: "t", state: "fault", reason: "reflex" },
        ],
      }),
      0,
      NOW,
    );
    expect(session).toEqual({ light: "red", reason: "fault", count: 1 });
  });
  test("a monitor that is offline or missing is not a red light", () => {
    expect(livePulse(live({ fr3: { state: "offline" } }), 0, NOW).light).toBe(
      "green",
    );
    expect(livePulse(live({ fr3: { state: "missing" } }), 0, NOW).light).toBe(
      "green",
    );
  });
  test("grey when the status cannot be read (not live, no answer, stale)", () => {
    expect(livePulse(null, 0, NOW).light).toBe("grey");
    expect(livePulse({ enabled: false }, 0, NOW).light).toBe("grey");
    expect(livePulse(live({ state: "annotating" }), 2, NOW).light).toBe("grey");
    expect(livePulse(live({ state: "annotating" }), 1, NOW).light).toBe("blue");
  });
  test("every state has a sentence in both languages", () => {
    for (const note of Object.values(PULSE_NOTES)) {
      expect(typeof (en as Record<string, string>)[note]).toBe("string");
      expect(typeof (zh as Record<string, string>)[note]).toBe("string");
    }
    expect((zh as Record<string, string>)["Open the live page"]).toBeTruthy();
  });
});

function harness(answers: (LiveStatusResponse | Error)[], visible = true) {
  const timers: { fn: () => void; ms: number; id: number }[] = [];
  let calls = 0;
  let isVisible = visible;
  let next = 0;
  const deps: PulseDeps = {
    fetchStatus: async () => {
      const answer = answers[Math.min(calls, answers.length - 1)];
      calls += 1;
      if (answer instanceof Error) throw answer;
      return answer;
    },
    visible: () => isVisible,
    setTimer: (fn, ms) => {
      next += 1;
      timers.push({ fn, ms, id: next });
      return next;
    },
    clearTimer: (handle) => {
      const at = timers.findIndex((t) => t.id === handle);
      if (at >= 0) timers.splice(at, 1);
    },
    now: () => 1234,
  };
  const store = new PulseStore(deps);
  return {
    store,
    timers,
    calls: () => calls,
    setVisible: (value: boolean) => {
      isVisible = value;
      store.visibilityChanged();
    },
  };
}
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("the status store", () => {
  test("a workspace that is not live is asked once and never polled", async () => {
    const h = harness([{ enabled: false }]);
    const off = h.store.subscribe(() => {});
    await settle();
    expect(h.calls()).toBe(1);
    expect(h.store.getSnapshot().enabled).toBe(false);
    expect(h.timers.length).toBe(0); // no timer, so no later request
    h.setVisible(false);
    h.setVisible(true); // back to the tab: still nothing to ask
    await settle();
    expect(h.timers.length).toBe(0);
    off();
    h.store.subscribe(() => {})(); // a new subscriber does not ask again
    await settle();
    expect(h.calls()).toBe(1);
  });
  test("a live workspace is refreshed every 8 s, and not while hidden", async () => {
    const h = harness([live(), live({ state: "annotating" })]);
    h.store.subscribe(() => {});
    await settle();
    expect(h.store.getSnapshot().enabled).toBe(true);
    expect(h.timers.map((t) => t.ms)).toEqual([PULSE_POLL_MS]);
    h.timers[0].fn();
    await settle();
    expect(h.calls()).toBe(2);
    expect(h.store.getSnapshot().status?.service?.state).toBe("annotating");
    h.setVisible(false); // hidden: the timer is dropped and nothing is asked
    expect(h.timers.length).toBe(0);
    await settle();
    expect(h.calls()).toBe(2);
    h.setVisible(true); // back: an immediate refresh, then the beat again
    await settle();
    expect(h.calls()).toBe(3);
    expect(h.timers.length).toBe(1);
  });
  test("failures back off, and a first failure does not decide anything", async () => {
    const h = harness([new Error("down"), new Error("down"), live()]);
    h.store.subscribe(() => {});
    await settle();
    expect(h.store.getSnapshot().enabled).toBeNull();
    expect(h.store.getSnapshot().failures).toBe(1);
    expect(h.timers.map((t) => t.ms)).toEqual([pulseDelay(1)]);
    h.timers[0].fn();
    await settle();
    expect(h.timers.map((t) => t.ms)).toEqual([pulseDelay(2)]);
    h.timers[0].fn();
    await settle();
    expect(h.store.getSnapshot().enabled).toBe(true);
    expect(h.store.getSnapshot().failures).toBe(0);
  });
  test("the delay doubles and is capped", () => {
    expect(pulseDelay(0)).toBe(8000);
    expect(pulseDelay(1)).toBe(16000);
    expect(pulseDelay(2)).toBe(30000);
    expect(pulseDelay(99)).toBe(30000);
  });
});
