import { describe, expect, test } from "bun:test";
import { foldPoll, pollDelay, pollEvaluating } from "../use-live";
import { POLL_ACTIVE_MS, POLL_IDLE_MS, clock, liveSource } from "../live-logic";
import { PULSE_OFF_MS } from "../live-pulse-store";
import type { LiveSessionsResponse, LiveStatusResponse } from "../types";

const empty = {
  status: null,
  sessions: null,
  error: "",
  failures: 0,
  lastOk: null,
  statusOkAt: null,
  sessionsOkAt: null,
  partial: "" as const,
  delay: POLL_IDLE_MS,
};
const ok = <T>(value: T): PromiseSettledResult<T> => ({
  status: "fulfilled",
  value,
});
const fail = (message: string): PromiseSettledResult<never> => ({
  status: "rejected",
  reason: new Error(message),
});
const status = (
  extra: Partial<LiveStatusResponse> = {},
): LiveStatusResponse => ({
  enabled: true,
  alive: true,
  service: { state: "idle", datasets: {}, fr3: { state: "ok" } },
  ...extra,
});
const sessions = (state: string, fr3 = "ok"): LiveSessionsResponse => ({
  enabled: true,
  sessions: [{ group: "m", task_folder: "t", state }],
  fr3: { state: fr3 },
});

describe("one poll folded into the page state", () => {
  test("one endpoint failing is said, never hidden behind a fresh time", () => {
    const first = foldPoll(empty, ok(status()), ok(sessions("running")), 1000);
    expect(first.partial).toBe("");
    const second = foldPoll(first, ok(status()), fail("HTTP 500"), 2000);
    expect(second.partial).toBe("sessions");
    expect(second.error).toBe("HTTP 500");
    expect(second.failures).toBe(0);
    expect(second.sessionsOkAt).toBe(1000);
    expect(second.statusOkAt).toBe(2000);
    // The last good session list is kept, not wiped.
    expect(second.sessions?.sessions?.[0].state).toBe("running");
  });

  test("both failing counts a failure and keeps the last snapshot", () => {
    const first = foldPoll(empty, ok(status()), ok(sessions("running")), 1000);
    const down = foldPoll(first, fail("offline"), fail("offline"), 2000);
    expect(down.failures).toBe(1);
    expect(down.status).toBe(first.status);
    expect(down.lastOk).toBe(1000);
    expect(down.delay).toBeGreaterThan(POLL_ACTIVE_MS);
  });

  test("a refresh that starts from the last state keeps it when the first request fails", () => {
    const first = foldPoll(empty, ok(status()), ok(sessions("finished")), 1000);
    // A refresh restarts the loop from `latest`, not from an empty state.
    const after = foldPoll(
      first,
      fail("HTTP 502"),
      ok(sessions("finished")),
      2000,
    );
    expect(after.status).toBe(first.status);
    expect(after.partial).toBe("status");
  });

  test("the displayed delay is the wait actually scheduled", () => {
    expect(pollDelay(status({ enabled: false }), false, 0)).toBe(PULSE_OFF_MS);
    expect(pollDelay(status(), false, 0)).toBe(POLL_IDLE_MS);
    const off = foldPoll(
      empty,
      ok(status({ enabled: false })),
      ok({ enabled: false }),
      1,
    );
    expect(off.delay).toBe(PULSE_OFF_MS);
  });

  test("a dead service's last 'annotating' does not keep fast polling", () => {
    const dead = status({
      alive: false,
      service: { state: "annotating", datasets: {} },
    });
    expect(pollEvaluating(dead, { enabled: true, sessions: [] })).toBe(false);
    expect(pollEvaluating({ ...dead, alive: true }, null)).toBe(true);
  });
});

describe("where sessions and the FR3 state come from", () => {
  test("from the endpoint that answered last", () => {
    const service = status({
      service: {
        state: "idle",
        datasets: {},
        fr3: { state: "red" },
        sessions: [{ group: "m", task_folder: "t", state: "fault" }],
      },
    });
    const own = sessions("running", "ok");
    // /sessions failed after /status reported the red light.
    const red = liveSource({
      status: service,
      sessions: own,
      statusOkAt: 2,
      sessionsOkAt: 1,
    });
    expect(red.fr3?.state).toBe("red");
    expect(red.sessions[0].state).toBe("fault");
    const fresh = liveSource({
      status: service,
      sessions: own,
      statusOkAt: 2,
      sessionsOkAt: 2,
    });
    expect(fresh.fr3?.state).toBe("ok");
  });
});

describe("a stopped service's copy is not the arm's state", () => {
  const copy = (alive: boolean) =>
    status({
      alive,
      service: {
        state: "idle",
        datasets: {},
        fr3: { state: "ok" },
        sessions: [{ group: "m", task_folder: "t", state: "running" }],
      },
    });
  test("with /sessions failing and the service stopped, nothing is claimed", () => {
    const source = liveSource({
      status: copy(false),
      sessions: null,
      statusOkAt: 5,
      sessionsOkAt: null,
    });
    expect(source.known).toBe(false);
    expect(source.fr3).toBeNull();
    expect(source.sessions).toEqual([]);
  });
  test("an alive service's copy is used when it answered last", () => {
    const source = liveSource({
      status: copy(true),
      sessions: sessions("finished", "red"),
      statusOkAt: 9,
      sessionsOkAt: 1,
    });
    expect(source.known).toBe(true);
    expect(source.fr3?.state).toBe("ok");
  });
  test("the client's own files still answer for a stopped service", () => {
    const source = liveSource({
      status: copy(false),
      sessions: sessions("running", "red"),
      statusOkAt: 9,
      sessionsOkAt: 1,
    });
    expect(source.known).toBe(true);
    expect(source.fr3?.state).toBe("red");
  });
});

describe("which source gave the arm's state", () => {
  const alive = status({
    alive: true,
    service: { state: "idle", datasets: {}, fr3: { state: "red" } },
  });
  test("the page can tell whether that source is the endpoint that is failing", () => {
    const own = liveSource({
      status: null,
      sessions: sessions("running", "ok"),
      statusOkAt: null,
      sessionsOkAt: 3,
    });
    expect(own.from).toBe("own");
    const service = liveSource({
      status: alive,
      sessions: sessions("running", "ok"),
      statusOkAt: 9,
      sessionsOkAt: 1,
    });
    expect([service.from, service.fr3?.state]).toEqual(["service", "red"]);
    const none = liveSource({
      status: status({ alive: false }),
      sessions: null,
      statusOkAt: 2,
      sessionsOkAt: null,
    });
    expect([none.from, none.known, none.fr3]).toEqual([null, false, null]);
  });
});

describe("times", () => {
  test("a time on another day carries its date", () => {
    const now = new Date(2026, 10, 14, 18, 0, 0);
    const today = new Date(2026, 10, 14, 9, 30, 0).getTime() / 1000;
    const yesterday = new Date(2026, 10, 13, 9, 30, 0).getTime() / 1000;
    expect(clock(today, now)).toBe(new Date(today * 1000).toLocaleTimeString());
    expect(clock(yesterday, now)).toContain(
      new Date(yesterday * 1000).toLocaleDateString(),
    );
  });
});
