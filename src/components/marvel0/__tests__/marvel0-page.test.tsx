import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { act, type ReactNode } from "react";
import { afterEach, describe, expect, mock, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { Marvel0CardView } from "../marvel0-card";
import {
  MARVEL0_POLL_MS,
  MARVEL0_REQUEST_MS,
  type Marvel0Card,
} from "../marvel0-logic";
import {
  healthPart,
  quietMarvel0,
  quietSetup,
  marvel0Status,
  setupStatus,
} from "./fixtures";

// This file is about the status cards. The action cards of steps 2 and 3 have
// their own tests (deploy-panel, command-panel, marvel0-actions-page), and
// the arm stack's controls and the hosted client's reads have theirs
// (stack-panel, run-panel); they are left out here so that their requests and
// timers are not mixed into these. The stand-ins still show the status cards
// the page hands to them, and the polls of the stack and the run read nothing,
// so the chain at the top follows the status cards.
const idlePoll = {
  loaded: false,
  value: null,
  current: false,
  problem: null,
  lastOk: null,
  refresh: async () => {},
};
mock.module("@/components/marvel0/use-stack-poll", () => ({
  STACK_FRESH_MS: 8000,
  useStackPoll: () => idlePoll,
}));
mock.module("@/components/marvel0/use-run-poll", () => ({
  RUN_POLL_MS: 1000,
  RUN_FRESH_MS: 5000,
  useRunPoll: () => ({ ...idlePoll, lastSeen: null }),
}));
mock.module("@/components/marvel0/stack-panel", () => ({
  StackPanel: ({ cards }: { cards: Marvel0Card[] }) => (
    <>
      {cards.map((card) => (
        <Marvel0CardView key={card.id} card={card} />
      ))}
    </>
  ),
}));
mock.module("@/components/marvel0/marvel0-actions", () => ({
  Marvel0Actions: ({
    policyStatus,
    clientStatus,
  }: {
    policyStatus?: ReactNode;
    clientStatus?: ReactNode;
  }) => (
    <>
      {policyStatus}
      {clientStatus}
    </>
  ),
}));
// Step 5 (the autonomy system) reads its own two routes and has its own tests
// (autonomy-*): a stand-in keeps its requests and timers out of these.
mock.module("@/components/marvel0/autonomy-step", () => ({
  AutonomyStep: () => null,
}));
const { default: Marvel0Page } = await import("@/app/marvel0/page");
setupDom();

const SETUP = "/api/levi/setup/status";
const MARVEL0 = "/api/levi/marvel0/status";

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
    if (ms !== undefined && ms >= MARVEL0_POLL_MS)
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
  const due = timers.filter((timer) => timer.ms === MARVEL0_REQUEST_MS);
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
  expect(timer.ms).not.toBe(MARVEL0_REQUEST_MS);
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
    Array.from(host.querySelectorAll("[data-testid^=marvel0-card-]")).map(
      (node) => [
        node.getAttribute("data-testid")!.replace("marvel0-card-", ""),
        node.getAttribute("data-state"),
      ],
    ),
  );
const loaded = (host: HTMLElement) =>
  waitFor(() => host.querySelector("[data-testid=marvel0-idle]"), {
    label: "the page to load",
  });

describe("the marvel0 page", () => {
  test("before the first answer it says Loading… once, in the body only", async () => {
    // Both requests stay open, so the page is still on its first round.
    const original = globalThis.fetch;
    globalThis.fetch = (() => new Promise<Response>(() => {})) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };
    const { host } = await render(<Marvel0Page />);
    expect(host.textContent!.split("Loading…")).toHaveLength(2);
    expect(host.querySelector("[aria-busy=true]")!.textContent).toBe(
      "Loading…",
    );
    const fresh = host.querySelector(".rg-fresh")!;
    expect(fresh.textContent).not.toContain("Loading…");
    // The rest of the line is there: how often it refreshes, and the button.
    expect(fresh.textContent).toContain("refreshes every 3 s");
    expect(fresh.textContent).not.toMatch(/^\s*·/);
    expect(host.querySelector("[data-testid=marvel0-idle]")).toBeNull();
  });

  test("a card leads to the offline FR3 3D preview, already before the first answer", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (() => new Promise<Response>(() => {})) as typeof fetch;
    restore = () => {
      globalThis.fetch = original;
    };
    const { host } = await render(<Marvel0Page />);
    const card = host.querySelector("[data-testid=marvel0-studio-link]")!;
    // It is there while the page still says "Loading…": it needs no status.
    expect(host.querySelector("[aria-busy=true]")).toBeTruthy();
    const link = card.querySelector("a")!;
    expect(link.getAttribute("href")).toBe("/robot-studio");
    expect(card.textContent).toContain(en["marvel0.studio.title"]);
    expect(card.textContent).toContain(en["marvel0.studio.note"]);
    // It says what the page is not: connected to a robot.
    expect(en["marvel0.studio.title"]).toMatch(/offline/i);
    expect(en["marvel0.studio.title"]).toMatch(/no robot connection/i);
    expect(zh["marvel0.studio.title"]).toContain("不连接机器人");
    // One link only: the card is a way to the page, not a second control.
    expect(card.querySelectorAll("a, button")).toHaveLength(1);
    // Between the head and the first of the steps, not after the steps.
    const kids = Array.from(host.querySelector("main")!.children);
    expect(kids.indexOf(card)).toBeGreaterThan(
      kids.indexOf(host.querySelector("header.rg-head")!),
    );
    expect(kids.indexOf(card)).toBeLessThan(
      kids.indexOf(host.querySelector("[aria-busy=true]")!),
    );
  });

  test("shows one card per component and the idle banner, from the two routes", async () => {
    const calls = serve(() => ({
      [SETUP]: setupStatus(),
      [MARVEL0]: marvel0Status(),
    }));
    const { host } = await render(<Marvel0Page />);
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
    const banner = host.querySelector("[data-testid=marvel0-idle]")!;
    expect(banner.getAttribute("data-state")).toBe("busy");
    expect(banner.textContent).toContain("Robot idle?");
    expect(banner.textContent).toContain("Looks busy");
    // Each reason names its source.
    expect(banner.textContent).toContain("Controller marker");
    expect(banner.textContent).toContain("Evaluation units (systemd)");
    expect(banner.textContent).toContain("Advisory only");
    // The answers came, so the line under the title never says "no answer".
    expect(host.textContent).not.toContain(en["marvel0.fresh.none"]);
    // The badge says the state in words.
    const gripper = host.querySelector("[data-testid=marvel0-card-gripper]")!;
    expect(gripper.textContent).toContain("Unknown");
    expect(gripper.textContent).toContain(
      "no status file, port or unit this page can read",
    );
    expect(gripper.textContent).toContain("terminal B");
    // Facts: the GPU line and the lock holder.
    const gpu = host.querySelector("[data-testid=marvel0-card-gpu]")!;
    expect(gpu.textContent).toContain("12.0 / 32.0 GiB");
    expect(gpu.textContent).toContain("levi: held by python (pid 4008)");
    // The line saying where a card's readings come from is the last row of
    // its Readings fold, said once, not a paragraph of its own under the card.
    const arm = host.querySelector("[data-testid=marvel0-card-arm]")!;
    const fold = arm.querySelector("details.rg-card__probe")!;
    expect(fold.querySelector(".rg-hint")).not.toBeNull();
    expect(arm.querySelectorAll(".rg-hint")).toHaveLength(1);
    expect(fold.lastElementChild!.classList.contains("rg-hint")).toBe(true);
    // Only the two LEVI routes were asked, as plain uncached GETs.
    expect(new Set(calls.map((call) => call.path))).toEqual(
      new Set([SETUP, MARVEL0]),
    );
    for (const call of calls) {
      expect(call.init?.method ?? "GET").toBe("GET");
      expect(call.init?.body).toBeUndefined();
      expect(call.init?.cache).toBe("no-store");
    }
  });

  test("the stack strip comes first, M to client, and says once why Desk, A and M are stale", async () => {
    serve(() => ({
      [SETUP]: setupStatus({
        fr3_health: healthPart({ state: "offline", age_s: 420 }),
      }),
      [MARVEL0]: marvel0Status(),
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const strip = host.querySelector("[data-testid=marvel0-strip]")!;
    const stops = Array.from(strip.querySelectorAll("li")).map((li) =>
      li.getAttribute("data-testid"),
    );
    expect(stops).toEqual([
      "marvel0-strip-M",
      "marvel0-strip-desk",
      "marvel0-strip-A",
      "marvel0-strip-B",
      "marvel0-strip-C",
      "marvel0-strip-policy",
      "marvel0-strip-run",
    ]);
    for (const id of ["M", "desk", "A"])
      expect(
        strip.querySelector(`[data-testid=marvel0-strip-${id}]`)!.textContent,
      ).toContain("Stale");
    const cause = host.querySelector("[data-testid=marvel0-strip-cause]")!;
    // The cause comes first, in the strip, above the tiles it explains.
    expect(cause.textContent).toMatch(/^Terminal M \(the health monitor\)/);
    expect(cause.textContent).toContain("Desk, A and M cannot update");
    expect(cause.textContent).toContain("7 min");
    expect(
      cause.compareDocumentPosition(strip.querySelector("ol")!) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Above every card.
    expect(
      cause.compareDocumentPosition(
        host.querySelector("[data-testid=marvel0-card-monitor]")!,
      ) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  test("cards A, B and C show no process evidence, even when the guard lists robot processes (RIG-01: passive sources only)", async () => {
    serve(() => ({
      [SETUP]: setupStatus({
        guard: {
          state: "ok",
          quiet: false,
          robot: [
            { pid: 11, name: "ros2_control_node" },
            { pid: 12, name: "ros2_control_node" },
            { pid: 13, name: "franka_server_ros2" },
          ],
          banner: null,
        },
      }),
      [MARVEL0]: marvel0Status(),
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    for (const id of ["arm", "gripper", "robot_server"]) {
      const node = host.querySelector(`[data-testid=marvel0-card-${id}]`)!;
      const text = node.textContent!;
      expect(text).not.toContain("Processes seen");
      expect(text).not.toContain("ros2_control_node");
      expect(text).not.toContain("franka_server");
    }
    // B stays unknown: nothing on the page guesses it from processes.
    expect(
      host
        .querySelector("[data-testid=marvel0-card-gripper]")!
        .getAttribute("data-state"),
    ).toBe("unknown");
  });

  test("the notes of a card are folded under a count, open only on a red card", async () => {
    serve(() => ({
      [SETUP]: setupStatus({
        ports: { state: "unknown", detail: "no socket table here" },
      }),
      [MARVEL0]: marvel0Status(),
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const folded = Array.from(host.querySelectorAll("details.rg-card__more"));
    expect(folded.length).toBeGreaterThan(0);
    for (const details of folded) {
      const card = details.closest("[data-state]")!;
      expect(details.hasAttribute("open")).toBe(
        card.getAttribute("data-state") === "red",
      );
      expect(details.querySelector("summary")!.textContent).toMatch(
        /^Details \(\d+\)$/,
      );
      expect(details.querySelector("ul.rg-notes li")).toBeTruthy();
    }
  });

  test("the scope says its point in one line; the long text is folded but on the page", async () => {
    serve(() => ({ [SETUP]: setupStatus(), [MARVEL0]: marvel0Status() }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const details = host.querySelector(".rg-scope-note details")!;
    expect(details.hasAttribute("open")).toBe(false);
    expect(details.querySelector("summary")!.textContent).toBe(
      en["marvel0.scope.more"],
    );
    expect(host.querySelector(".rg-scope-note strong")!.textContent).toBe(
      en["marvel0.scope.lead"],
    );
  });

  test("says what is read-only, that the status cards never connect to robot ports, what the gates are worth and that nothing is an e-stop; the only button outside the chain refreshes", async () => {
    serve(() => ({ [SETUP]: setupStatus(), [MARVEL0]: marvel0Status() }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const text = host.textContent!;
    expect(text).toContain(en["marvel0.scope.read_only"]);
    expect(text).toContain(en["marvel0.scope.no_connect"]);
    expect(text).toContain(en["marvel0.scope.actions"]);
    expect(text).toContain(en["marvel0.scope.gates"]);
    expect(text).toContain(en["marvel0.scope.no_estop"]);
    expect(en["marvel0.scope.read_only"]).toMatch(
      /^The status cards are read-only/,
    );
    expect(en["marvel0.scope.no_connect"]).toContain("never connect");
    // The one connection the page makes itself is named, not hidden.
    expect(en["marvel0.scope.no_connect"]).toContain("FCI");
    // The gates are not sold as a barrier against another program.
    expect(en["marvel0.scope.gates"]).toContain("do not stop another program");
    expect(en["marvel0.scope.no_estop"]).toContain("emergency stop");
    // Nothing the page says is that it never starts anything.
    expect(en["marvel0.scope.lead"]).not.toMatch(/never start|only watch/i);
    // The chain's seven nodes are buttons that move to a card; apart from
    // them the only button is Refresh.
    const strip = host.querySelector("[data-testid=marvel0-strip]")!;
    expect(strip.querySelectorAll("button")).toHaveLength(7);
    const buttons = Array.from(host.querySelectorAll("button"))
      .filter((b) => !strip.contains(b))
      .map((b) => b.textContent?.trim());
    expect(buttons).toEqual(["Refresh"]);
    // No link, form or field, except the one card to the offline 3D preview.
    const others = Array.from(
      host.querySelectorAll("a, form, input, select"),
    ).filter((el) => !el.closest("[data-testid=marvel0-studio-link]"));
    expect(others).toHaveLength(0);
  });

  test("a marvel0 with nothing running: not started, unknown, and idle", async () => {
    serve(() => ({ [SETUP]: quietSetup(), [MARVEL0]: quietMarvel0() }));
    const { host } = await render(<Marvel0Page />);
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
    const banner = host.querySelector("[data-testid=marvel0-idle]")!;
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
      [MARVEL0]: {
        ...quietMarvel0(),
        units: { state: "unknown", items: [], detail: "systemctl failed" },
      },
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const banner = host.querySelector("[data-testid=marvel0-idle]")!;
    expect(banner.getAttribute("data-state")).toBe("unknown");
    expect(banner.textContent).toContain("Cannot tell");
    expect(banner.textContent).toContain(en["marvel0.banner.idle_unverified"]);
    expect(banner.textContent).not.toContain("No robot activity seen");
  });

  test("an idle answer that lists a reason says it contradicts itself, not that a part was unread", async () => {
    serve(() => ({
      [SETUP]: quietSetup(),
      [MARVEL0]: {
        ...quietMarvel0(),
        robot_idle: {
          state: "idle",
          reasons: [
            {
              source: "marker",
              state: "busy",
              text: "the arm-controller marker is present (for 2 min)",
            },
          ],
        },
      },
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const banner = host.querySelector("[data-testid=marvel0-idle]")!;
    expect(banner.getAttribute("data-state")).toBe("unknown");
    expect(banner.textContent).toContain(
      en["marvel0.banner.idle_with_reasons"],
    );
    expect(banner.textContent).not.toContain(
      en["marvel0.banner.idle_unverified"],
    );
  });

  describe("a health monitor that is not running", () => {
    const REASONS = [
      "controller_manager_unreachable 43s",
      "robot_state_no_data 44s",
    ];
    const offline = () =>
      setupStatus({
        fr3_health: {
          state: "offline",
          detail: "the monitor stopped updating",
          age_s: 43,
          hardware_active: false,
          controller_active: false,
          reasons: REASONS,
        },
      });
    const markerOnly = {
      robot_idle: {
        state: "busy",
        reasons: [
          {
            source: "marker",
            state: "busy",
            text: "the arm-controller marker is present (for 30 min)",
            code: "marker.present",
            params: { age_s: 1800 },
          },
        ],
      },
    };
    const card = (host: HTMLElement, id: string) =>
      host.querySelector(`[data-testid=marvel0-card-${id}]`)!;

    test("a marker as the only reason gets one hint line under it", async () => {
      serve(() => ({
        [SETUP]: offline(),
        [MARVEL0]: marvel0Status(markerOnly),
      }));
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      const banner = host.querySelector("[data-testid=marvel0-idle]")!;
      expect(banner.getAttribute("data-state")).toBe("busy");
      expect(banner.querySelectorAll("li")).toHaveLength(1);
      // One line of its own, after the reason and before the advisory.
      const hint = banner.querySelector("[data-testid=marvel0-idle-hint]")!;
      expect(hint.textContent).toBe(en["marvel0.hint.marker_leftover"]);
      expect(hint.previousElementSibling!.tagName).toBe("UL");
      expect(hint.nextElementSibling!.textContent).toContain("Advisory only");
    });

    test("the hint is in Chinese on a Chinese page", async () => {
      localStorage.setItem("levi-language", "zh");
      serve(() => ({
        [SETUP]: offline(),
        [MARVEL0]: marvel0Status(markerOnly),
      }));
      const { host } = await render(
        <LocaleProvider>
          <Marvel0Page />
        </LocaleProvider>,
      );
      await loaded(host);
      await waitFor(
        () =>
          host
            .querySelector("[data-testid=marvel0-idle]")!
            .textContent!.includes(zh["marvel0.hint.marker_leftover"]),
        { label: "the Chinese hint" },
      );
      expect(
        host.querySelector("[data-testid=marvel0-idle]")!.textContent,
      ).not.toContain(en["marvel0.hint.marker_leftover"]);
    });

    test("no hint while the health monitor is running", async () => {
      serve(() => ({
        [SETUP]: setupStatus(),
        [MARVEL0]: marvel0Status(markerOnly),
      }));
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      expect(host.textContent).toContain("Controller marker");
      expect(host.textContent).not.toContain(
        en["marvel0.hint.marker_leftover"],
      );
    });

    test("no hint when a unit or a session is busy as well", async () => {
      serve(() => ({ [SETUP]: offline(), [MARVEL0]: marvel0Status() }));
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      expect(host.textContent).toContain("Controller marker");
      expect(host.textContent).not.toContain(
        en["marvel0.hint.marker_leftover"],
      );
    });

    test("the reasons are on the Terminal M card; the Desk and Terminal A cards point to it", async () => {
      serve(() => ({ [SETUP]: offline(), [MARVEL0]: marvel0Status() }));
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      for (const reason of REASONS)
        expect(card(host, "monitor").textContent).toContain(reason);
      for (const id of ["desk", "arm"]) {
        const text = card(host, id).textContent!;
        expect(text).toContain(en["marvel0.note.same_as_monitor"]);
        // The server's sentence stays, the reason lines do not repeat.
        expect(text).toContain("the monitor stopped updating");
        for (const reason of REASONS) expect(text).not.toContain(reason);
      }
      expect(card(host, "monitor").textContent).not.toContain(
        en["marvel0.note.same_as_monitor"],
      );
    });

    test("with a red light all three cards list the reasons", async () => {
      serve(() => ({
        [SETUP]: setupStatus({
          fr3_health: {
            state: "red",
            reasons: REASONS,
            hardware_active: true,
            controller_active: true,
            robot_mode_name: "Idle",
            robot_mode: 1,
            age_s: 0.3,
          },
        }),
        [MARVEL0]: marvel0Status(),
      }));
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      for (const id of ["desk", "arm", "monitor"]) {
        const text = card(host, id).textContent!;
        for (const reason of REASONS) expect(text).toContain(reason);
        expect(text).not.toContain(en["marvel0.note.same_as_monitor"]);
      }
    });
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
    serve(() => ({
      [SETUP]: setupStatus(),
      [MARVEL0]: marvel0Status(REASONS),
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const items = host.querySelectorAll("[data-testid=marvel0-idle] li");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain(en["marvel0.source.units"]);
    expect(items[0].textContent).toContain(en["marvel0.reason.busy"]);
    expect(items[1].textContent).toContain(en["marvel0.source.guard"]);
    expect(items[1].textContent).toContain(en["marvel0.reason.unknown"]);
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
    serve(() => ({
      [SETUP]: setupStatus(),
      [MARVEL0]: marvel0Status(REASONS),
    }));
    const { host } = await render(
      <LocaleProvider>
        <Marvel0Page />
      </LocaleProvider>,
    );
    await loaded(host);
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=marvel0-idle]")!
          .textContent!.includes(zh["marvel0.source.units"]),
      { label: "the Chinese labels" },
    );
    const items = host.querySelectorAll("[data-testid=marvel0-idle] li");
    expect(items[0].textContent).toContain(zh["marvel0.reason.busy"]);
    expect(items[1].textContent).toContain(zh["marvel0.reason.unknown"]);
    const server = items[0].querySelector("[lang=en]")!;
    expect(server.textContent).toBe(REASONS.robot_idle.reasons[0].text);
    expect(items[0].textContent).not.toContain(en["marvel0.source.units"]);
    expect(items[0].textContent).not.toContain(en["marvel0.reason.busy"]);
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
    serve(() => ({ [SETUP]: setupStatus(), [MARVEL0]: marvel0Status(CODED) }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const items = host.querySelectorAll("[data-testid=marvel0-idle] li");
    expect(items).toHaveLength(4);
    expect(
      host.querySelector("[data-testid=marvel0-idle] [lang=en]"),
    ).toBeNull();
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
    serve(() => ({ [SETUP]: setupStatus(), [MARVEL0]: marvel0Status(CODED) }));
    const { host } = await render(
      <LocaleProvider>
        <Marvel0Page />
      </LocaleProvider>,
    );
    await loaded(host);
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=marvel0-idle]")!
          .textContent!.includes(zh["marvel0.source.units"]),
      { label: "the Chinese labels" },
    );
    const items = host.querySelectorAll("[data-testid=marvel0-idle] li");
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
      [MARVEL0]: marvel0Status({ robot_idle: { state: "busy", reasons } }),
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const items = host.querySelectorAll("[data-testid=marvel0-idle] li");
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
      [MARVEL0]: marvel0Status({
        robot_idle: {
          state: "busy",
          reasons: [guard, units, marker, sessions],
        },
      }),
    }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const items = Array.from(
      host.querySelectorAll("[data-testid=marvel0-idle] li"),
    );
    expect(
      items.map((item) =>
        item.textContent!.includes(en["marvel0.reason.busy"]),
      ),
    ).toEqual([true, true, true, false]);
  });

  test("a part that cannot be read is worded from its code in the card's notes", async () => {
    localStorage.setItem("levi-language", "zh");
    serve(() => ({
      [SETUP]: setupStatus(),
      [MARVEL0]: marvel0Status({
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
        <Marvel0Page />
      </LocaleProvider>,
    );
    await loaded(host);
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=marvel0-card-policy]")!
          .textContent!.includes(zh["marvel0.reason.units_unreadable"]),
      { label: "the Chinese note" },
    );
    const policy = host.querySelector("[data-testid=marvel0-card-policy]")!;
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
      [MARVEL0]: marvel0Status(),
    };
    serve(() => answers);
    const { host } = await render(<Marvel0Page />);
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
      const node = host.querySelector(`[data-testid=marvel0-card-${id}]`)!;
      expect(node.textContent).toContain(en["marvel0.state.red"]);
      expect(node.textContent).toContain("Fault");
      expect(node.textContent).toContain("red light");
    }
  });

  test("a failing marvel0 route leaves the machine cards and turns the banner to cannot tell", async () => {
    serve(() => ({ [SETUP]: setupStatus() }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const banner = host.querySelector("[data-testid=marvel0-idle]")!;
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
      [MARVEL0]: marvel0Status(),
    }));
    const { host } = await render(<Marvel0Page />);
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
    const server = host.querySelector(
      "[data-testid=marvel0-card-robot_server]",
    )!;
    expect(server.textContent).toContain("42 s ago");
    expect(server.textContent).toContain(en["marvel0.fact.in_sample"]);
    expect(host.textContent).toContain(en["marvel0.note.stale_sample"]);
  });

  test("the failure of a request is announced as a status", async () => {
    serve(() => ({ [SETUP]: setupStatus() }));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    const failed = host.querySelector(".rg-fresh .rg-bad")!;
    expect(failed.textContent).toContain("last request failed");
    expect(failed.getAttribute("role")).toBe("status");
  });

  test("both routes failing: everything unknown, with the failure said", async () => {
    serve(() => ({}));
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    for (const state of Object.values(states(host)))
      expect(state).toBe("unknown");
    expect(host.textContent).toContain(en["marvel0.note.no_setup"]);
    // No answer at all is not "Loading…": the first round has finished.
    expect(host.querySelector(".rg-fresh")!.textContent).toContain(
      en["marvel0.fresh.none"],
    );
    expect(host.textContent).not.toContain("Loading…");
  });
});

describe("refreshing", () => {
  test("every 3 s while visible, nothing while hidden, at once when it returns", async () => {
    const calls = serve(() => ({
      [SETUP]: setupStatus(),
      [MARVEL0]: marvel0Status(),
    }));
    captureTimers();
    const { host } = await render(<Marvel0Page />);
    await loaded(host);
    expect(calls).toHaveLength(2);
    // The next round is scheduled 3 s ahead.
    expect(timers.map((timer) => timer.ms)).toEqual([MARVEL0_POLL_MS]);
    await fireTimer();
    expect(calls).toHaveLength(4);
    expect(timers.map((timer) => timer.ms)).toEqual([MARVEL0_POLL_MS]);

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
            [MARVEL0]: marvel0Status(),
          },
    );
    captureTimers();
    const realNow = Date.now;
    let now = 2_000_000;
    Date.now = () => now;
    try {
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      // 6 s old on arrival is still inside the window.
      expect(states(host).robot_server).toBe("ok");

      failing = true;
      now += 5000;
      await fireTimer();
      // 5 s on screen and 6 s before: 11 s. The marvel0 status has no age and is
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
        ? { [SETUP]: HANG, [MARVEL0]: HANG }
        : { [SETUP]: setupStatus(), [MARVEL0]: marvel0Status() },
    );
    captureTimers();
    const { host } = await render(<Marvel0Page />);
    // Still on the first round, with a deadline on each request.
    expect(calls).toHaveLength(2);
    expect(host.querySelector("[aria-busy=true]")).not.toBeNull();
    expect(timers.map((timer) => timer.ms)).toEqual([
      MARVEL0_REQUEST_MS,
      MARVEL0_REQUEST_MS,
    ]);
    const signals = calls.map((call) => call.init!.signal!);
    expect(signals.map((signal) => signal.aborted)).toEqual([false, false]);

    await fireDeadlines();
    await loaded(host);
    // The page leaves "Loading…", says why, and the requests were told to stop.
    expect(host.querySelector("[aria-busy=true]")).toBeNull();
    expect(host.querySelector(".rg-bad")!.textContent).toContain(
      `no answer within ${MARVEL0_REQUEST_MS / 1000} s`,
    );
    expect(signals.map((signal) => signal.aborted)).toEqual([true, true]);
    for (const state of Object.values(states(host)))
      expect(state).toBe("unknown");
    // Both failed: the wait doubles, and nothing else is pending.
    expect(timers.map((timer) => timer.ms)).toEqual([2 * MARVEL0_POLL_MS]);

    // The server is back: the next round shows it.
    hang = false;
    await fireTimer();
    expect(calls).toHaveLength(4);
    await waitFor(() => states(host).robot_server === "ok", {
      label: "the answers after the hang",
    });
    expect(timers.map((timer) => timer.ms)).toEqual([MARVEL0_POLL_MS]);
  });

  test("a hung round expires the answer on screen when it is too old", async () => {
    let hang = false;
    const calls = serve(() =>
      hang
        ? { [SETUP]: HANG, [MARVEL0]: HANG }
        : { [SETUP]: setupStatus(), [MARVEL0]: marvel0Status() },
    );
    captureTimers();
    const realNow = Date.now;
    let now = 2_000_000;
    Date.now = () => now;
    try {
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      // A good round leaves no deadline behind.
      expect(timers.map((timer) => timer.ms)).toEqual([MARVEL0_POLL_MS]);
      expect(states(host).robot_server).toBe("ok");

      hang = true;
      now += 4000;
      await fireTimer();
      expect(calls).toHaveLength(4);
      // Waiting on the hung round, the answer is 4 s old and still shown.
      expect(states(host).robot_server).toBe("ok");

      now += MARVEL0_REQUEST_MS;
      await fireDeadlines();
      expect(states(host).robot_server).toBe("unknown");
      expect(states(host).gpu).toBe("unknown");
      expect(host.textContent).toContain("last request failed");
      expect(timers.map((timer) => timer.ms)).toEqual([2 * MARVEL0_POLL_MS]);
    } finally {
      Date.now = realNow;
    }
  });

  test("Refresh gives up the hung round at once: only the new round has deadlines", async () => {
    const calls = serve(() => ({ [SETUP]: HANG, [MARVEL0]: HANG }));
    captureTimers();
    const { host } = await render(<Marvel0Page />);
    expect(timers).toHaveLength(2);
    const first = calls.map((call) => call.init!.signal!);
    const button = Array.from(host.querySelectorAll("button")).find(
      (node) => node.textContent?.trim() === "Refresh",
    )!;
    await click(button);
    await waitFor(() => calls.length === 4, { label: "the refresh" });
    expect(first.map((signal) => signal.aborted)).toEqual([true, true]);
    expect(timers.map((timer) => timer.ms)).toEqual([
      MARVEL0_REQUEST_MS,
      MARVEL0_REQUEST_MS,
    ]);
    // The abandoned round did not turn into a failure on the page.
    expect(host.querySelector(".rg-bad")).toBeNull();
  });

  test("Refresh asks again at once", async () => {
    const calls = serve(() => ({
      [SETUP]: setupStatus(),
      [MARVEL0]: marvel0Status(),
    }));
    const { host } = await render(<Marvel0Page />);
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
      failing ? {} : { [SETUP]: setupStatus(), [MARVEL0]: marvel0Status() },
    );
    captureTimers();
    const realNow = Date.now;
    let now = 2_000_000;
    Date.now = () => now;
    try {
      const { host } = await render(<Marvel0Page />);
      await loaded(host);
      expect(states(host).robot_server).toBe("ok");

      failing = true;
      now += 4000;
      await fireTimer();
      expect(calls).toHaveLength(4);
      // Still shown: the answer is 4 s old.
      expect(states(host).robot_server).toBe("ok");
      expect(host.textContent).toContain("last request failed");

      now += MARVEL0_REQUEST_MS;
      // The retry after a round of failures waits longer than 3 s.
      expect(timers.map((timer) => timer.ms)).toEqual([2 * MARVEL0_POLL_MS]);
      await fireTimer();
      expect(calls).toHaveLength(6);
      expect(states(host).robot_server).toBe("unknown");
      expect(states(host).gpu).toBe("unknown");
    } finally {
      Date.now = realNow;
    }
  });
});
