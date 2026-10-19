import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, mock, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { RIG_POLL_MS, RIG_REQUEST_MS } from "../rig-logic";
import { quietRig, quietSetup, rigStatus, setupStatus } from "./fixtures";

// This file is about the status cards. The action section at the end of the
// page has its own tests (deploy-panel, command-panel, rig-actions-page); it is
// left out here so that its requests and timers are not mixed into these.
mock.module("@/components/rig/rig-actions", () => ({
  RigActions: () => null,
}));
const { default: RigPage } = await import("@/app/rig/page");
setupDom();

const SETUP = "/api/levi/setup/status";
const RIG = "/api/levi/rig/status";

type Answer = unknown | Response;
let restore = () => {};
let timers: Array<{
  run: () => void;
  ms: number;
  handle: ReturnType<typeof setTimeout>;
}> = [];

/** An answer that never comes, and a request that ignores its signal. */
const HANG = Symbol("hang");

/** Both routes answer from `answers`; a missing answer is a 404, `HANG`
 * never settles. */
function serve(answers: () => Record<string, Answer>) {
  const original = globalThis.fetch;
  const calls: Array<{ path: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const path = String(input);
    calls.push({ path, init });
    const answer = answers()[path];
    if (answer === HANG) return new Promise<Response>(() => {});
    if (answer instanceof Response) return answer;
    if (answer === undefined) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(answer), { status: 200 });
  }) as typeof fetch;
  restore = () => {
    globalThis.fetch = original;
  };
  return calls;
}

/** Records the page's polling timers and request deadlines (3 s and longer;
 * the tests' own waits are shorter) so a test fires them by hand. One that
 * the page clears is forgotten. */
function captureTimers() {
  const real = globalThis.setTimeout;
  const realClear = globalThis.clearTimeout;
  globalThis.setTimeout = ((
    fn: () => void,
    ms?: number,
    ...rest: unknown[]
  ) => {
    const handle = real(fn as never, ms, ...(rest as []));
    if (ms !== undefined && ms >= RIG_POLL_MS)
      timers.push({ run: fn, ms, handle });
    return handle;
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((handle?: ReturnType<typeof setTimeout>) => {
    timers = timers.filter((timer) => timer.handle !== handle);
    realClear(handle);
  }) as typeof clearTimeout;
  const undo = restore;
  restore = () => {
    globalThis.setTimeout = real;
    globalThis.clearTimeout = realClear;
    undo();
  };
}

/** The deadlines of the requests that are open, as the clock would fire them. */
async function fireDeadlines() {
  const due = timers.filter((timer) => timer.ms === RIG_REQUEST_MS);
  expect(due.length).toBeGreaterThan(0);
  await act(async () => {
    for (const timer of due) {
      timers = timers.filter((other) => other !== timer);
      timer.run();
    }
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
  return due.length;
}

/** Fires the newest recorded timer, as the clock would, and lets the round
 * it starts settle. */
async function fireTimer() {
  const timer = timers.pop()!;
  expect(timer.ms).not.toBe(RIG_REQUEST_MS);
  await act(async () => {
    timer.run();
    await new Promise((resolve) => setTimeout(resolve, 30));
  });
  return timer;
}

function visibility(state: "hidden" | "visible") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => {
  restore();
  restore = () => {};
  timers = [];
  delete (document as unknown as Record<string, unknown>).visibilityState;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const states = (host: HTMLElement) =>
  Object.fromEntries(
    Array.from(host.querySelectorAll("[data-testid^=rig-card-]")).map(
      (node) => [
        node.getAttribute("data-testid")!.replace("rig-card-", ""),
        node.getAttribute("data-state"),
      ],
    ),
  );
const loaded = (host: HTMLElement) =>
  waitFor(() => host.querySelector("[data-testid=rig-idle]"), {
    label: "the page to load",
  });

describe("the Rig console page", () => {
  test("before the first answer it says Loading… once, in the body only", async () => {
    // Both requests stay open, so the page is still on its first round.
    const original = globalThis.fetch;
    globalThis.fetch = (() => new Promise<Response>(() => {})) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };
    const { host } = await render(<RigPage />);
    expect(host.textContent!.split("Loading…")).toHaveLength(2);
    expect(host.querySelector("[aria-busy=true]")!.textContent).toBe(
      "Loading…",
    );
    const fresh = host.querySelector(".rg-fresh")!;
    expect(fresh.textContent).not.toContain("Loading…");
    // The rest of the line is there: how often it refreshes, and the button.
    expect(fresh.textContent).toContain("refreshes every 3 s");
    expect(fresh.textContent).not.toMatch(/^\s*·/);
    expect(host.querySelector("[data-testid=rig-idle]")).toBeNull();
  });

  test("shows one card per component and the idle banner, from the two routes", async () => {
    const calls = serve(() => ({
      [SETUP]: setupStatus(),
      [RIG]: rigStatus(),
    }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    expect(states(host)).toEqual({
      desk: "ok",
      arm: "ok",
      gripper: "unknown",
      robot_server: "ok",
      monitor: "ok",
      policy: "ok",
      demo: "ok",
      live: "ok",
      vlm: "ok",
      gpu: "ok",
    });
    const banner = host.querySelector("[data-testid=rig-idle]")!;
    expect(banner.getAttribute("data-state")).toBe("busy");
    expect(banner.textContent).toContain("Robot idle?");
    expect(banner.textContent).toContain("Looks busy");
    // Each reason names its source.
    expect(banner.textContent).toContain("Controller marker");
    expect(banner.textContent).toContain("Evaluation units (systemd)");
    expect(banner.textContent).toContain("Advisory only");
    // The answers came, so the line under the title never says "no answer".
    expect(host.textContent).not.toContain(en["rig.fresh.none"]);
    // The badge says the state in words.
    const gripper = host.querySelector("[data-testid=rig-card-gripper]")!;
    expect(gripper.textContent).toContain("Unknown");
    expect(gripper.textContent).toContain("No passive source");
    expect(gripper.textContent).toContain("terminal B");
    // Facts: the GPU line and the lock holder.
    const gpu = host.querySelector("[data-testid=rig-card-gpu]")!;
    expect(gpu.textContent).toContain("12.0 / 32.0 GiB");
    expect(gpu.textContent).toContain("levi: held by python (pid 4008)");
    // Only the two LEVI routes were asked, as plain uncached GETs.
    expect(new Set(calls.map((call) => call.path))).toEqual(
      new Set([SETUP, RIG]),
    );
    for (const call of calls) {
      expect(call.init?.method ?? "GET").toBe("GET");
      expect(call.init?.body).toBeUndefined();
      expect(call.init?.cache).toBe("no-store");
    }
  });

  test("says it is read-only, never connects to robot ports and is no e-stop; the only button refreshes", async () => {
    serve(() => ({ [SETUP]: setupStatus(), [RIG]: rigStatus() }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const text = host.textContent!;
    expect(text).toContain(en["rig.scope.read_only"]);
    expect(text).toContain(en["rig.scope.no_connect"]);
    expect(text).toContain(en["rig.scope.actions"]);
    expect(text).toContain(en["rig.scope.no_estop"]);
    expect(en["rig.scope.read_only"]).toMatch(
      /^The status cards are read-only/,
    );
    expect(en["rig.scope.no_connect"]).toContain("never connects");
    expect(en["rig.scope.no_estop"]).toContain("emergency stop");
    const buttons = Array.from(host.querySelectorAll("button")).map((b) =>
      b.textContent?.trim(),
    );
    expect(buttons).toEqual(["Refresh"]);
    expect(host.querySelector("a, form, input, select")).toBeNull();
  });

  test("a rig with nothing running: not started, unknown, and idle", async () => {
    serve(() => ({ [SETUP]: quietSetup(), [RIG]: quietRig() }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    expect(states(host)).toMatchObject({
      desk: "unknown",
      arm: "unknown",
      robot_server: "not_started",
      monitor: "not_started",
      policy: "not_started",
      demo: "not_started",
      vlm: "ok",
    });
    const banner = host.querySelector("[data-testid=rig-idle]")!;
    expect(banner.getAttribute("data-state")).toBe("idle");
    // It says what was not seen, in a neutral tone: never a success badge.
    expect(banner.textContent).toContain("No robot activity seen");
    expect(banner.querySelector(".ds-badge--neutral")).not.toBeNull();
    expect(banner.querySelector(".ds-badge--success")).toBeNull();
    expect(host.textContent).toContain("Not started");
  });

  test("an idle answer over a part nobody could read is shown as cannot tell", async () => {
    serve(() => ({
      [SETUP]: quietSetup(),
      [RIG]: {
        ...quietRig(),
        units: { state: "unknown", items: [], detail: "systemctl failed" },
      },
    }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const banner = host.querySelector("[data-testid=rig-idle]")!;
    expect(banner.getAttribute("data-state")).toBe("unknown");
    expect(banner.textContent).toContain("Cannot tell");
    expect(banner.textContent).toContain(en["rig.banner.idle_unverified"]);
    expect(banner.textContent).not.toContain("No robot activity seen");
  });

  const REASONS = {
    robot_idle: {
      state: "busy",
      reasons: [
        {
          source: "units",
          state: "busy",
          text: "levi-aeri-policy-pi05.service is active (running)",
        },
        {
          source: "guard",
          state: "unknown",
          text: "the quiet guard could not tell whether a robot process runs",
        },
      ],
    },
  };

  test("each reason is worded from the catalogue, with the server's sentence kept apart", async () => {
    serve(() => ({ [SETUP]: setupStatus(), [RIG]: rigStatus(REASONS) }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const items = host.querySelectorAll("[data-testid=rig-idle] li");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain(en["rig.source.units"]);
    expect(items[0].textContent).toContain(en["rig.reason.busy"]);
    expect(items[1].textContent).toContain(en["rig.source.guard"]);
    expect(items[1].textContent).toContain(en["rig.reason.unknown"]);
    // The English sentence is a separate, marked line.
    for (const [index, reason] of REASONS.robot_idle.reasons.entries()) {
      const server = items[index].querySelector("[lang=en]")!;
      expect(server.textContent).toBe(reason.text);
      expect(server.classList.contains("rg-server")).toBe(true);
    }
    // The server's state word is not shown as it is.
    expect(items[0].textContent).not.toMatch(/\bbusy\b/);
  });

  test("in Chinese the labels are Chinese and only the server's sentence stays English", async () => {
    localStorage.setItem("levi-language", "zh");
    serve(() => ({ [SETUP]: setupStatus(), [RIG]: rigStatus(REASONS) }));
    const { host } = await render(
      <LocaleProvider>
        <RigPage />
      </LocaleProvider>,
    );
    await loaded(host);
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=rig-idle]")!
          .textContent!.includes(zh["rig.source.units"]),
      { label: "the Chinese labels" },
    );
    const items = host.querySelectorAll("[data-testid=rig-idle] li");
    expect(items[0].textContent).toContain(zh["rig.reason.busy"]);
    expect(items[1].textContent).toContain(zh["rig.reason.unknown"]);
    const server = items[0].querySelector("[lang=en]")!;
    expect(server.textContent).toBe(REASONS.robot_idle.reasons[0].text);
    expect(items[0].textContent).not.toContain(en["rig.source.units"]);
    expect(items[0].textContent).not.toContain(en["rig.reason.busy"]);
  });

  const CODED = {
    robot_idle: {
      state: "busy",
      reasons: [
        {
          source: "marker",
          state: "busy",
          text: "SERVER the arm-controller marker is present (for 2 min)",
          code: "marker.present",
          params: { age_s: 95 },
        },
        {
          source: "eval_sessions",
          state: "busy",
          text: "SERVER evaluation session 'pi05_run_a' updated 12 s ago",
          code: "sessions.recent",
          params: { group: "pi05_run_a", age_s: 12, status: "running" },
        },
        {
          source: "units",
          state: "busy",
          text: "SERVER levi-aeri-policy-pi05.service is active (running)",
          code: "units.active",
          params: {
            name: "levi-aeri-policy-pi05.service",
            active: "active",
            sub: "running",
          },
        },
        {
          source: "guard",
          state: "unknown",
          text: "SERVER the quiet guard found no robot process",
          code: "guard.not_quiet",
          params: {},
        },
      ],
    },
  };

  test("a reason whose code the page knows is worded by the page, with no English sentence of the server's", async () => {
    serve(() => ({ [SETUP]: setupStatus(), [RIG]: rigStatus(CODED) }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const items = host.querySelectorAll("[data-testid=rig-idle] li");
    expect(items).toHaveLength(4);
    expect(host.querySelector("[data-testid=rig-idle] [lang=en]")).toBeNull();
    expect(host.textContent).not.toContain("SERVER");
    for (const item of items)
      expect(
        item.querySelector(".rg-what")!.textContent!.length,
      ).toBeGreaterThan(10);
    expect(items[0].textContent).toContain("2 min ago");
    expect(items[1].textContent).toContain("pi05_run_a");
    expect(items[2].textContent).toContain("levi-aeri-policy-pi05.service");
  });

  test("in Chinese a coded reason has no English but the values the server gave", async () => {
    localStorage.setItem("levi-language", "zh");
    serve(() => ({ [SETUP]: setupStatus(), [RIG]: rigStatus(CODED) }));
    const { host } = await render(
      <LocaleProvider>
        <RigPage />
      </LocaleProvider>,
    );
    await loaded(host);
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=rig-idle]")!
          .textContent!.includes(zh["rig.source.units"]),
      { label: "the Chinese labels" },
    );
    const items = host.querySelectorAll("[data-testid=rig-idle] li");
    expect(items).toHaveLength(4);
    // The values the server gave, and the product name in the label of a source.
    const values = [
      "pi05_run_a",
      "levi-aeri-policy-pi05.service",
      "running",
      "active",
      "systemd",
    ];
    for (const item of items) {
      expect(item.querySelector("[lang=en]")).toBeNull();
      const bare = values.reduce(
        (text, value) => text.split(value).join(""),
        item.textContent!,
      );
      expect(bare).not.toMatch(/[A-Za-z]/);
    }
    expect(items[0].textContent).toContain("2分钟前");
  });

  test("a code the page does not know shows the server's sentence, marked as English", async () => {
    const reasons = [
      {
        source: "units",
        state: "busy",
        text: "SERVER a kind of reason this page has no words for",
        code: "units.from_the_future",
        params: { name: "x" },
      },
      ...CODED.robot_idle.reasons.slice(0, 1),
    ];
    serve(() => ({
      [SETUP]: setupStatus(),
      [RIG]: rigStatus({ robot_idle: { state: "busy", reasons } }),
    }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const items = host.querySelectorAll("[data-testid=rig-idle] li");
    const server = items[0].querySelector("[lang=en]")!;
    expect(server.textContent).toBe(reasons[0].text);
    expect(server.classList.contains("rg-server")).toBe(true);
    expect(items[0].querySelector(".rg-what")).toBeNull();
    // The reason the page knows next to it carries none.
    expect(items[1].querySelector("[lang=en]")).toBeNull();
    expect(items[1].querySelector(".rg-what")).not.toBeNull();
  });

  test("busy reasons are listed before the ones that cannot tell", async () => {
    const [marker, sessions, units, guard] = CODED.robot_idle.reasons;
    serve(() => ({
      [SETUP]: setupStatus(),
      [RIG]: rigStatus({
        robot_idle: {
          state: "busy",
          reasons: [guard, units, marker, sessions],
        },
      }),
    }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const items = Array.from(
      host.querySelectorAll("[data-testid=rig-idle] li"),
    );
    expect(
      items.map((item) => item.textContent!.includes(en["rig.reason.busy"])),
    ).toEqual([true, true, true, false]);
  });

  test("a part that cannot be read is worded from its code in the card's notes", async () => {
    localStorage.setItem("levi-language", "zh");
    serve(() => ({
      [SETUP]: setupStatus(),
      [RIG]: rigStatus({
        units: { state: "unknown", items: [], detail: "SERVER units failed" },
        robot_idle: {
          state: "unknown",
          reasons: [
            {
              source: "units",
              state: "unknown",
              text: "SERVER units failed",
              code: "units.unreadable",
              params: {},
            },
          ],
        },
      }),
    }));
    const { host } = await render(
      <LocaleProvider>
        <RigPage />
      </LocaleProvider>,
    );
    await loaded(host);
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=rig-card-policy]")!
          .textContent!.includes(zh["rig.reason.units_unreadable"]),
      { label: "the Chinese note" },
    );
    const policy = host.querySelector("[data-testid=rig-card-policy]")!;
    expect(policy.textContent).not.toContain("SERVER");
    expect(policy.querySelector("[lang=en]")).toBeNull();
  });

  test("a red light and a stale monitor are told apart", async () => {
    const answers = {
      [SETUP]: setupStatus({
        fr3_health: {
          state: "red",
          reasons: ["cartesian reflex"],
          hardware_active: true,
          controller_active: true,
          robot_mode_name: "Reflex",
          robot_mode: 4,
          age_s: 0.3,
        },
      }),
      [RIG]: rigStatus(),
    };
    serve(() => answers);
    const { host } = await render(<RigPage />);
    await loaded(host);
    expect(states(host)).toMatchObject({
      desk: "red",
      arm: "red",
      monitor: "red",
    });
    expect(host.textContent).toContain("cartesian reflex");
    // The badge says Fault; the sentence on the cards that mirror the monitor
    // still names the red light.
    for (const id of ["desk", "monitor"]) {
      const node = host.querySelector(`[data-testid=rig-card-${id}]`)!;
      expect(node.textContent).toContain(en["rig.state.red"]);
      expect(node.textContent).toContain("Fault");
      expect(node.textContent).toContain("red light");
    }
  });

  test("a failing rig route leaves the machine cards and turns the banner to cannot tell", async () => {
    serve(() => ({ [SETUP]: setupStatus() }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const banner = host.querySelector("[data-testid=rig-idle]")!;
    expect(banner.getAttribute("data-state")).toBe("unknown");
    expect(banner.textContent).toContain("Cannot tell");
    expect(banner.textContent).toContain("HTTP 404");
    expect(states(host)).toMatchObject({
      robot_server: "ok",
      gpu: "ok",
      demo: "unknown",
    });
    expect(host.textContent).toContain("last request failed");
  });

  test("an old sample of the machine status makes its cards stale, with its age", async () => {
    serve(() => ({
      [SETUP]: setupStatus({ stale: true, age_s: 42 }),
      [RIG]: rigStatus(),
    }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    expect(states(host)).toEqual({
      desk: "stale",
      arm: "stale",
      gripper: "unknown",
      robot_server: "stale",
      monitor: "stale",
      policy: "stale",
      demo: "ok",
      live: "stale",
      vlm: "stale",
      gpu: "stale",
    });
    const server = host.querySelector("[data-testid=rig-card-robot_server]")!;
    expect(server.textContent).toContain("42 s ago");
    expect(server.textContent).toContain(en["rig.fact.in_sample"]);
    expect(host.textContent).toContain(en["rig.note.stale_sample"]);
  });

  test("the failure of a request is announced as a status", async () => {
    serve(() => ({ [SETUP]: setupStatus() }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    const failed = host.querySelector(".rg-fresh .rg-bad")!;
    expect(failed.textContent).toContain("last request failed");
    expect(failed.getAttribute("role")).toBe("status");
  });

  test("both routes failing: everything unknown, with the failure said", async () => {
    serve(() => ({}));
    const { host } = await render(<RigPage />);
    await loaded(host);
    for (const state of Object.values(states(host)))
      expect(state).toBe("unknown");
    expect(host.textContent).toContain(en["rig.note.no_setup"]);
    // No answer at all is not "Loading…": the first round has finished.
    expect(host.querySelector(".rg-fresh")!.textContent).toContain(
      en["rig.fresh.none"],
    );
    expect(host.textContent).not.toContain("Loading…");
  });
});

describe("refreshing", () => {
  test("every 3 s while visible, nothing while hidden, at once when it returns", async () => {
    const calls = serve(() => ({ [SETUP]: setupStatus(), [RIG]: rigStatus() }));
    captureTimers();
    const { host } = await render(<RigPage />);
    await loaded(host);
    expect(calls).toHaveLength(2);
    // The next round is scheduled 3 s ahead.
    expect(timers.map((timer) => timer.ms)).toEqual([RIG_POLL_MS]);
    await fireTimer();
    expect(calls).toHaveLength(4);
    expect(timers.map((timer) => timer.ms)).toEqual([RIG_POLL_MS]);

    // Hidden: the pending timer is cancelled, and were it to fire anyway it
    // asks nothing and schedules nothing.
    const pending = timers[0];
    await act(async () => visibility("hidden"));
    expect(timers).toHaveLength(0);
    await act(async () => {
      pending.run();
      await new Promise((resolve) => setTimeout(resolve, 30));
    });
    expect(calls).toHaveLength(4);
    expect(timers).toHaveLength(0);

    // Visible again: an immediate round.
    await act(async () => visibility("visible"));
    await waitFor(() => calls.length === 6, { label: "the round on return" });
  });

  test("the age a sample already had when it arrived counts towards dropping it", async () => {
    let failing = false;
    serve(() =>
      failing
        ? {}
        : {
            [SETUP]: setupStatus({ age_s: 6, cached: true }),
            [RIG]: rigStatus(),
          },
    );
    captureTimers();
    const realNow = Date.now;
    let now = 2_000_000;
    Date.now = () => now;
    try {
      const { host } = await render(<RigPage />);
      await loaded(host);
      // 6 s old on arrival is still inside the window.
      expect(states(host).robot_server).toBe("ok");

      failing = true;
      now += 5000;
      await fireTimer();
      // 5 s on screen and 6 s before: 11 s. The rig status has no age and is
      // only 5 s old.
      expect(states(host).robot_server).toBe("unknown");
      expect(states(host).demo).toBe("ok");
    } finally {
      Date.now = realNow;
    }
  });

  test("a request that never settles is given up at its deadline, and polling goes on", async () => {
    let hang = true;
    const calls = serve(() =>
      hang
        ? { [SETUP]: HANG, [RIG]: HANG }
        : { [SETUP]: setupStatus(), [RIG]: rigStatus() },
    );
    captureTimers();
    const { host } = await render(<RigPage />);
    // Still on the first round, with a deadline on each request.
    expect(calls).toHaveLength(2);
    expect(host.querySelector("[aria-busy=true]")).not.toBeNull();
    expect(timers.map((timer) => timer.ms)).toEqual([
      RIG_REQUEST_MS,
      RIG_REQUEST_MS,
    ]);
    const signals = calls.map((call) => call.init!.signal!);
    expect(signals.map((signal) => signal.aborted)).toEqual([false, false]);

    await fireDeadlines();
    await loaded(host);
    // The page leaves "Loading…", says why, and the requests were told to stop.
    expect(host.querySelector("[aria-busy=true]")).toBeNull();
    expect(host.querySelector(".rg-bad")!.textContent).toContain(
      `no answer within ${RIG_REQUEST_MS / 1000} s`,
    );
    expect(signals.map((signal) => signal.aborted)).toEqual([true, true]);
    for (const state of Object.values(states(host)))
      expect(state).toBe("unknown");
    // Both failed: the wait doubles, and nothing else is pending.
    expect(timers.map((timer) => timer.ms)).toEqual([2 * RIG_POLL_MS]);

    // The server is back: the next round shows it.
    hang = false;
    await fireTimer();
    expect(calls).toHaveLength(4);
    await waitFor(() => states(host).robot_server === "ok", {
      label: "the answers after the hang",
    });
    expect(timers.map((timer) => timer.ms)).toEqual([RIG_POLL_MS]);
  });

  test("a hung round expires the answer on screen when it is too old", async () => {
    let hang = false;
    const calls = serve(() =>
      hang
        ? { [SETUP]: HANG, [RIG]: HANG }
        : { [SETUP]: setupStatus(), [RIG]: rigStatus() },
    );
    captureTimers();
    const realNow = Date.now;
    let now = 2_000_000;
    Date.now = () => now;
    try {
      const { host } = await render(<RigPage />);
      await loaded(host);
      // A good round leaves no deadline behind.
      expect(timers.map((timer) => timer.ms)).toEqual([RIG_POLL_MS]);
      expect(states(host).robot_server).toBe("ok");

      hang = true;
      now += 4000;
      await fireTimer();
      expect(calls).toHaveLength(4);
      // Waiting on the hung round, the answer is 4 s old and still shown.
      expect(states(host).robot_server).toBe("ok");

      now += RIG_REQUEST_MS;
      await fireDeadlines();
      expect(states(host).robot_server).toBe("unknown");
      expect(states(host).gpu).toBe("unknown");
      expect(host.textContent).toContain("last request failed");
      expect(timers.map((timer) => timer.ms)).toEqual([2 * RIG_POLL_MS]);
    } finally {
      Date.now = realNow;
    }
  });

  test("Refresh gives up the hung round at once: only the new round has deadlines", async () => {
    const calls = serve(() => ({ [SETUP]: HANG, [RIG]: HANG }));
    captureTimers();
    const { host } = await render(<RigPage />);
    expect(timers).toHaveLength(2);
    const first = calls.map((call) => call.init!.signal!);
    const button = Array.from(host.querySelectorAll("button")).find(
      (node) => node.textContent?.trim() === "Refresh",
    )!;
    await click(button);
    await waitFor(() => calls.length === 4, { label: "the refresh" });
    expect(first.map((signal) => signal.aborted)).toEqual([true, true]);
    expect(timers.map((timer) => timer.ms)).toEqual([
      RIG_REQUEST_MS,
      RIG_REQUEST_MS,
    ]);
    // The abandoned round did not turn into a failure on the page.
    expect(host.querySelector(".rg-bad")).toBeNull();
  });

  test("Refresh asks again at once", async () => {
    const calls = serve(() => ({ [SETUP]: setupStatus(), [RIG]: rigStatus() }));
    const { host } = await render(<RigPage />);
    await loaded(host);
    expect(calls).toHaveLength(2);
    const button = Array.from(host.querySelectorAll("button")).find(
      (node) => node.textContent?.trim() === "Refresh",
    )!;
    await click(button);
    await waitFor(() => calls.length === 4, { label: "the refresh" });
  });

  test("a sample is kept through a short failure and dropped when it is too old", async () => {
    let failing = false;
    const calls = serve(() =>
      failing ? {} : { [SETUP]: setupStatus(), [RIG]: rigStatus() },
    );
    captureTimers();
    const realNow = Date.now;
    let now = 2_000_000;
    Date.now = () => now;
    try {
      const { host } = await render(<RigPage />);
      await loaded(host);
      expect(states(host).robot_server).toBe("ok");

      failing = true;
      now += 4000;
      await fireTimer();
      expect(calls).toHaveLength(4);
      // Still shown: the answer is 4 s old.
      expect(states(host).robot_server).toBe("ok");
      expect(host.textContent).toContain("last request failed");

      now += RIG_REQUEST_MS;
      // The retry after a round of failures waits longer than 3 s.
      expect(timers.map((timer) => timer.ms)).toEqual([2 * RIG_POLL_MS]);
      await fireTimer();
      expect(calls).toHaveLength(6);
      expect(states(host).robot_server).toBe("unknown");
      expect(states(host).gpu).toBe("unknown");
    } finally {
      Date.now = realNow;
    }
  });
});
