import { act } from "react";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { errorOf, type ApiResult } from "../deploy-api";
import type { RunApi } from "../run-api";
import { RunDock } from "../run-dock";
import { useRunActions } from "../use-run-actions";
import { useRunPoll } from "../use-run-poll";
import { tick } from "./actions-helpers";
import { RUN_VIEWS, runDoc } from "./ctl-fixtures";

// The HOLD dock: the bar with HOLD that follows the window while the arm may
// move and the Run card's own HOLD is out of view.

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

type Doc = ReturnType<typeof runDoc>;

function fake(initial: Doc) {
  const state = {
    doc: initial as Doc | null,
    order: [] as string[],
    bodies: [] as Array<Record<string, unknown>>,
  };
  const write = (op: string) => async (body: unknown) => {
    state.order.push(op);
    state.bodies.push(body as Record<string, unknown>);
    return { ok: true, status: 202, data: { done: true } } as ApiResult;
  };
  const api: RunApi = {
    read: async () =>
      state.doc
        ? { ok: true, status: 200, data: structuredClone(state.doc) }
        : errorOf(503, {}),
    challenge: write("challenge"),
    start: write("start"),
    key: write("key"),
    hold: write("hold"),
    end: write("end"),
    log: async () => errorOf(404, {}),
  };
  return { api, state };
}

function Harness({ api }: { api: RunApi }) {
  const poll = useRunPoll(api, { pollMs: 25 });
  const actions = useRunActions(api, poll);
  return <RunDock poll={poll} actions={actions} />;
}

const MAY_MOVE = new Set([
  "starting",
  "at_gate",
  "homing",
  "running",
  "awaiting_label",
  "finishing",
  "lost",
]);
const dock = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-run-dock]");
const dockHold = (host: HTMLElement) =>
  host.querySelector(
    "[data-testid=marvel0-run-dock-hold]",
  ) as HTMLButtonElement;

async function open(
  name: string,
  zhLanguage = false,
  change?: (doc: ReturnType<typeof runDoc>) => void,
) {
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const f = fake(runDoc(name, change));
  const element = <Harness api={f.api} />;
  const view = await render(
    zhLanguage ? <LocaleProvider>{element}</LocaleProvider> : element,
  );
  await tick(4, 40);
  return { ...view, ...f };
}

/** A stand-in for the Run card's HOLD, at a place in the window. */
function cardHold(top: number, bottom: number) {
  const el = document.createElement("button");
  el.setAttribute("data-testid", "marvel0-run-hold");
  el.getBoundingClientRect = () =>
    ({
      top,
      bottom,
      left: 0,
      right: 100,
      width: 100,
      height: bottom - top,
    }) as DOMRect;
  document.body.appendChild(el);
  return el;
}
const scrolled = () =>
  act(async () => {
    window.dispatchEvent(new Event("scroll"));
  });

describe("the HOLD dock", () => {
  test("is there in every view of the backend in which the arm may move or the client is frozen, and not in the others", async () => {
    for (const name of RUN_VIEWS) {
      const doc = runDoc(name);
      const { host } = await open(name);
      const expected =
        MAY_MOVE.has(doc.state) ||
        doc.may_move === true ||
        doc.state === "held";
      expect([name, dock(host) !== null]).toEqual([name, expected]);
      // HOLD itself is only offered while there is something to hold.
      expect([name, dockHold(host) !== null]).toEqual([
        name,
        MAY_MOVE.has(doc.state) || doc.may_move === true,
      ]);
    }
    // One render per backend view: 5 s was too tight under the merge tier's load.
  }, 30000);

  test("a frozen client keeps the dock: it says so (and why), points at the Run card and offers no second HOLD", async () => {
    for (const name of ["held", "held_monitor_red"]) {
      const { host } = await open(name);
      const bar = dock(host)!;
      expect(bar.className).toContain("rg-dock--frozen");
      expect(bar.textContent).toContain(en["marvel0.run.state.held"]);
      // The backend's reason, said once (it already says nothing resumes).
      expect(
        host.querySelector("[data-testid=marvel0-run-dock-frozen]")!
          .textContent,
      ).toBe(
        en[
          name === "held"
            ? "marvel0.run.reason.held_person"
            : "marvel0.run.reason.held_monitor_red"
        ],
      );
      expect(dockHold(host)).toBeNull();
      const go = host.querySelector(
        "[data-testid=marvel0-run-dock-go]",
      ) as HTMLButtonElement;
      expect(go.textContent).toContain(en["marvel0.run.dock.go"]);
      // It moves to the Run card.
      const target = document.createElement("div");
      target.id = "marvel0-node-run";
      let moved = 0;
      target.scrollIntoView = () => {
        moved += 1;
      };
      document.body.appendChild(target);
      await click(go);
      expect([name, moved]).toEqual([name, 1]);
      target.remove();
    }
  });

  test("a frozen client without a reason gets the plain sentence", async () => {
    const { host } = await open("held", false, (doc) => {
      doc.reason = null;
    });
    expect(
      host.querySelector("[data-testid=marvel0-run-dock-frozen]")!.textContent,
    ).toBe(en["marvel0.run.hold_done"]);
  });

  test("says the state, how far the episode is, and that it is not an emergency stop", async () => {
    const { host } = await open("running");
    const bar = dock(host)!;
    expect(bar.textContent).toContain(en["marvel0.run.state.running"]);
    expect(
      host.querySelector("[data-testid=marvel0-run-dock-at]")!.textContent,
    ).toContain("Episode 1 of 5");
    expect(
      host.querySelector("[data-testid=marvel0-run-dock-at]")!.textContent,
    ).toContain("Step 137 of 400");
    expect(bar.textContent).toContain("Not an emergency stop");
    expect(bar.textContent).toContain("physical e-stop");
    expect(dockHold(host)!.textContent).toContain(en["marvel0.run.hold"]);
    expect(dockHold(host)!.disabled).toBe(false);
  });

  test("a client that is lost: HOLD is still offered, and the line beside it says Hold may not reach the client", async () => {
    const { host } = await open("lost");
    expect(dockHold(host)).not.toBeNull();
    const note = host.querySelector("[data-testid=marvel0-run-dock-lost]")!;
    expect(note.textContent).toBe(en["marvel0.run.dock.lost"]);
    expect(en["marvel0.run.dock.lost"]).toBe(
      "Lost: Hold may not reach the client; use the physical e-stop.",
    );
    expect(zh["marvel0.run.dock.lost"]).toBe(
      "已失联：保持可能到不了客户端，请用物理急停。",
    );
    // A client that runs keeps the ordinary line.
    const running = await open("running");
    expect(
      running.host.querySelector("[data-testid=marvel0-run-dock-lost]"),
    ).toBeNull();
  });

  test("sends the same hold as the card: at once, with nothing but a request id", async () => {
    const { host, state } = await open("running");
    await click(dockHold(host)!);
    expect(state.order).toEqual(["hold"]);
    expect(Object.keys(state.bodies[0])).toEqual(["request_id"]);
  });

  test("is gone while the card's own HOLD is on screen, and back when it is scrolled out", async () => {
    const card = cardHold(200, 264);
    const { host } = await open("running");
    await scrolled();
    await waitFor(() => (dock(host) === null ? true : null), {
      label: "the dock hides",
    });
    // Scrolled so that the card's HOLD is above the window ...
    card.getBoundingClientRect = () =>
      ({
        top: -300,
        bottom: -236,
        left: 0,
        right: 100,
        width: 100,
        height: 64,
      }) as DOMRect;
    await scrolled();
    await waitFor(() => dock(host), { label: "the dock shows" });
    // ... and when it sits under the dock's own place it still counts as out.
    card.getBoundingClientRect = () =>
      ({
        top: window.innerHeight - 40,
        bottom: window.innerHeight + 24,
        left: 0,
        right: 100,
        width: 100,
        height: 64,
      }) as DOMRect;
    await scrolled();
    await tick(2, 20);
    expect(dock(host)).not.toBeNull();
    card.remove();
  });

  test("a frozen client's dock is gone while the card's frozen frame (with End) is on screen: the dock only points there", async () => {
    const frame = document.createElement("div");
    frame.setAttribute("data-testid", "marvel0-run-frozen");
    frame.getBoundingClientRect = () =>
      ({
        top: 200,
        bottom: 260,
        left: 0,
        right: 100,
        width: 100,
        height: 60,
      }) as DOMRect;
    document.body.appendChild(frame);
    const { host } = await open("held");
    await scrolled();
    await waitFor(() => (dock(host) === null ? true : null), {
      label: "the dock hides",
    });
    frame.remove();
  });

  test("stays when the answers stop, and says the read is old: the client may still be running", async () => {
    const { host, state } = await open("running");
    state.doc = null;
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-run-dock-stale]"),
      { label: "the stale note" },
    );
    expect(dock(host)).not.toBeNull();
    expect(dockHold(host)!.disabled).toBe(false);
  });

  test("is in Chinese with the same catalogue words, and the Chinese says it is not an emergency stop", async () => {
    const { host } = await open("running", true);
    const bar = dock(host)!;
    expect(bar.textContent).toContain(zh["marvel0.run.hold"]);
    expect(bar.textContent).toContain(zh["marvel0.run.dock.note"]);
    expect(zh["marvel0.run.dock.note"]).toMatch(/不是急停/);
    expect(bar.getAttribute("aria-label")).toBe(zh["marvel0.run.dock.label"]);
  });

  test("is pinned by sticky at the end of the page, below the cards, on the dock's z-index token", () => {
    const css = readFileSync(
      join(import.meta.dir, "../marvel0-dock.css"),
      "utf8",
    );
    expect(css).toMatch(/\.rg-dock\s*{[^}]*position:\s*sticky/);
    expect(css).toMatch(/\.rg-dock\s*{[^}]*bottom:\s*var\(--ds-space-3\)/);
    expect(css).toMatch(/\.rg-dock\s*{[^}]*z-index:\s*var\(--ds-z-dock\)/);
    expect(css).toMatch(/\.rg-dock \.rg-dock__hold\s*{[^}]*min-height:\s*64px/);
    // Tokens only: no colour literal.
    expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(
      /#[0-9a-f]{3,8}\b|rgba?\(/i,
    );
  });
});
