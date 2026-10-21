import { readFileSync } from "fs";
import { join } from "path";
import { afterEach, describe, expect, test } from "bun:test";
import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { AutonomyBoundary, AutonomyStepLazy } from "../autonomy-lazy";
import { AutonomyStep, useCardStopOnScreen } from "../autonomy-step";
import type { AeriFact } from "../autonomy-logic";
import { $, FAST, serve, text, wait } from "./autonomy-helpers";
import { tick } from "./actions-helpers";
import { fakeAutonomy, readyDoc, runDoc } from "./autonomy-fixtures";
import en from "@/i18n/en.json";

// How often Step 5 reads, and what it loads when (review R11 M4 and L2): the
// readiness document costs the backend a handful of processes per read, so it
// is not read every 3 s when hosting is off, and not at all while an evaluation
// client is live (the arm controller is up then); the dock's one-second timer
// runs only while a D is alive; the step is loaded on demand.

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});
const E = en as Record<string, string>;
const root = join(import.meta.dir, "../../../..");
const source = (file: string) => readFileSync(join(root, "src", file), "utf8");

/** Reads per second the page makes of each document over a window. */
async function readsIn(f: ReturnType<typeof fakeAutonomy>, ms: number) {
  const before = { ...f.state.reads };
  await wait(ms);
  return {
    ready: f.state.reads.ready - before.ready,
    run: f.state.reads.run - before.run,
  };
}

async function open(
  f: ReturnType<typeof fakeAutonomy>,
  props: { evalLive?: boolean; onRun?: (fact: AeriFact | null) => void } = {},
) {
  const element = (evalLive: boolean) => (
    <AutonomyStep
      api={f.api}
      limits={FAST}
      evalLive={evalLive}
      onRun={props.onRun}
    />
  );
  const view = await render(element(props.evalLive ?? false));
  await waitFor(() => f.state.reads.run > 0 && f.state.reads.ready > 0, {
    label: "the first reads",
  });
  return { ...view, element };
}

describe("the readiness document is read slowly where there is nothing to prepare", () => {
  test("at the normal pace while hosting is on", async () => {
    const f = fakeAutonomy({ ready: readyDoc("prepared_ready") });
    await open(f);
    const n = await readsIn(f, 400);
    // FAST: 25 ms between reads. Far more than the slow pace allows (400 ms).
    expect(n.ready).toBeGreaterThanOrEqual(5);
  });

  test("switched off in the control file: one read, then the slow pace (and D's document too)", async () => {
    const f = fakeAutonomy({
      ready: readyDoc("disabled"),
      run: runDoc("idle_disabled"),
    });
    await open(f);
    await tick(3, 10); // the pace change takes effect
    const n = await readsIn(f, 600);
    expect(n.ready).toBeLessThanOrEqual(2);
    expect(n.run).toBeLessThanOrEqual(2);
    expect(text($(document.body, "marvel0-autonomy-disabled"))).toContain(
      E["marvel0.autonomy.disabled.off"],
    );
  });

  test("no usable section in the site file: the same", async () => {
    for (const name of ["unconfigured", "invalid_section"]) {
      const f = fakeAutonomy({ ready: readyDoc(name), run: runDoc("idle") });
      await open(f);
      await tick(3, 10);
      const n = await readsIn(f, 500);
      expect([name, n.ready <= 2]).toEqual([name, true]);
    }
  });

  test("a refusal `aeri_disabled` of the route is the same as a document that says off", async () => {
    const f = fakeAutonomy({ ready: null, run: null });
    const refusal = {
      ok: false as const,
      status: 403,
      code: "aeri_disabled",
      message: "x",
      extra: {},
      field: null,
      fields: [],
    };
    f.state.readyFail = refusal;
    f.state.runFail = refusal;
    await open(f);
    await tick(3, 10);
    const n = await readsIn(f, 500);
    expect(n.ready).toBeLessThanOrEqual(2);
    expect(n.run).toBeLessThanOrEqual(2);
  });

  test("a D that is alive is read every second whatever the switch says", async () => {
    const f = fakeAutonomy({
      ready: readyDoc("disabled"),
      run: runDoc("forward_running"),
    });
    await open(f);
    await tick(3, 10);
    const n = await readsIn(f, 400);
    expect(n.run).toBeGreaterThanOrEqual(5);
  });
});

describe("while an evaluation client is live", () => {
  test("the readiness document is read once and then not at all; D's document slowly; both come back when the client ends", async () => {
    const f = fakeAutonomy({
      ready: readyDoc("prepared_ready"),
      run: runDoc("idle"),
    });
    const view = await open(f, { evalLive: true });
    await tick(3, 10);
    const n = await readsIn(f, 500);
    expect(n.ready).toBe(0);
    expect(n.run).toBeLessThanOrEqual(2);
    // Said on the card, and Start is off for the client.
    expect(text($(view.host, "marvel0-autonomy-paused"))).toBe(
      E["marvel0.autonomy.ready.paused"],
    );
    expect(text($(view.host, "marvel0-autonomy-start-off"))).toContain(
      E["marvel0.autonomy.local.eval_live"],
    );
    // The client ends.
    await view.rerender(view.element(false));
    await tick(3, 30);
    const after = await readsIn(f, 300);
    expect(after.ready).toBeGreaterThanOrEqual(3);
    expect($(view.host, "marvel0-autonomy-paused")).toBeNull();
  });
});

describe("the dock's watch runs only while a D is alive", () => {
  /** Every interval and scroll listener the hook starts, and whether it was
   * stopped again. */
  function spy() {
    const set = globalThis.setInterval;
    const clear = globalThis.clearInterval;
    const live = new Set<unknown>();
    let started = 0;
    globalThis.setInterval = ((
      fn: () => void,
      ms?: number,
      ...rest: unknown[]
    ) => {
      const id = (set as (...a: unknown[]) => unknown)(fn, ms, ...rest);
      if (ms === 1000) {
        live.add(id);
        started += 1;
      }
      return id;
    }) as typeof setInterval;
    globalThis.clearInterval = ((id: unknown) => {
      live.delete(id);
      return (clear as (i: unknown) => void)(id);
    }) as typeof clearInterval;
    const listeners = { scroll: 0, resize: 0 };
    const add = window.addEventListener.bind(window);
    const remove = window.removeEventListener.bind(window);
    window.addEventListener = ((type: string, ...rest: unknown[]) => {
      if (type in listeners) listeners[type as "scroll"] += 1;
      return (add as (...a: unknown[]) => void)(type, ...rest);
    }) as typeof window.addEventListener;
    window.removeEventListener = ((type: string, ...rest: unknown[]) => {
      if (type in listeners) listeners[type as "scroll"] -= 1;
      return (remove as (...a: unknown[]) => void)(type, ...rest);
    }) as typeof window.removeEventListener;
    return {
      live: () => live.size,
      started: () => started,
      listeners: () => ({ ...listeners }),
      restore: () => {
        globalThis.setInterval = set;
        globalThis.clearInterval = clear;
        window.addEventListener = add;
        window.removeEventListener = remove;
      },
    };
  }
  function Probe({ active }: { active: boolean }) {
    return <p data-testid="on-screen">{String(useCardStopOnScreen(active))}</p>;
  }

  test("nothing is started while no D lives: no one-second timer, no scroll or resize listener", async () => {
    const s = spy();
    try {
      await render(<Probe active={false} />);
      await tick(2, 5);
      expect(s.started()).toBe(0);
      expect(s.listeners()).toEqual({ scroll: 0, resize: 0 });
    } finally {
      s.restore();
    }
  });

  test("one timer and the listeners while D lives; all of them stopped when it is gone", async () => {
    const s = spy();
    try {
      const view = await render(<Probe active={true} />);
      await tick(2, 5);
      expect(s.live()).toBe(1);
      expect(s.listeners()).toEqual({ scroll: 1, resize: 1 });
      await view.rerender(<Probe active={false} />);
      await tick(2, 5);
      expect(s.live()).toBe(0);
      expect(s.listeners()).toEqual({ scroll: 0, resize: 0 });
      expect(text($(view.host, "on-screen"))).toBe("false");
    } finally {
      s.restore();
    }
  });

  test("in the step: no scroll or resize watch while nothing is hosted, one while D lives, none after it ended", async () => {
    const s = spy();
    try {
      const f = fakeAutonomy({
        ready: readyDoc("prepared_ready"),
        run: runDoc("idle"),
      });
      await open(f);
      await tick(3, 10);
      expect(s.listeners()).toEqual({ scroll: 0, resize: 0 });
      await serve(f, runDoc("forward_running"), 8);
      expect($(document.body, "marvel0-autonomy-dock") !== null).toBe(true);
      expect(s.listeners()).toEqual({ scroll: 1, resize: 1 });
      await serve(f, runDoc("ended_done"), 8);
      expect(s.listeners()).toEqual({ scroll: 0, resize: 0 });
    } finally {
      s.restore();
    }
  });
});

describe("what the page tells the chain", () => {
  test("a change of D's state is one call; many reads of the same facts are none", async () => {
    const f = fakeAutonomy({
      ready: readyDoc("prepared_ready"),
      run: runDoc("await_release"),
    });
    const seen: Array<AeriFact | null> = [];
    await open(f, { onRun: (fact) => seen.push(fact) });
    await tick(8, 30); // many reads of the same document
    const last = (): AeriFact | null => seen[seen.length - 1];
    expect(last()).toEqual({
      alive: true,
      state: "await_release",
      current: true,
    });
    const calls = seen.length;
    expect(calls).toBeLessThanOrEqual(3);
    await serve(f, runDoc("forward_running"), 8);
    expect(last()).toEqual({
      alive: true,
      state: "forward_running",
      current: true,
    });
    await serve(f, runDoc("ended_done"), 8);
    expect(last()).toEqual({ alive: false, state: "ended", current: true });
  });
});

describe("Step 5 is loaded on demand, and a failure of it does not take the page down", () => {
  test("the page names the lazy wrapper and never the step itself; the wrapper imports the step on demand", () => {
    const page = source("app/marvel0/page.tsx");
    expect(page).toContain('from "@/components/marvel0/autonomy-lazy"');
    expect(page).not.toContain('from "@/components/marvel0/autonomy-step"');
    const lazy = source("components/marvel0/autonomy-lazy.tsx");
    expect(lazy).toMatch(/lazy\(\(\) =>\s*import\("\.\/autonomy-step"\)/);
    // Only a type comes from the step at load time.
    expect(lazy).toContain(
      'import type { AutonomyStep } from "./autonomy-step"',
    );
  });

  test("a loading line first, then the step; its Stop is on the page as before", async () => {
    const f = fakeAutonomy({
      ready: readyDoc("prepared_ready"),
      run: runDoc("forward_running"),
    });
    const view = await render(<AutonomyStepLazy api={f.api} limits={FAST} />);
    // (the module may already be loaded by an earlier test of this file)
    await waitFor(() => $(view.host, "marvel0-step-autonomy"), {
      label: "step 5 to load",
    });
    await waitFor(() => $(view.host, "marvel0-autonomy-stop"), {
      label: "its Stop",
    });
    expect($(view.host, "marvel0-autonomy-load-failed")).toBeNull();
  });

  test("the lazy wrapper has the boundary: a step that throws while it renders leaves the rest of the page standing", async () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      const f = fakeAutonomy({});
      const view = await render(
        <main>
          <p data-testid="rest">the rest of the page</p>
          {/* limits null: the step throws on its first render */}
          <AutonomyStepLazy api={f.api} limits={null as never} />
        </main>,
      );
      await waitFor(() => $(view.host, "marvel0-autonomy-load-failed"), {
        label: "the note of a step that failed",
      });
      expect(text($(view.host, "rest"))).toBe("the rest of the page");
    } finally {
      console.error = quiet;
    }
  });

  test("a step that throws is replaced by one note that names the way out; the rest of the page is untouched", async () => {
    function Broken(): never {
      throw new Error("the step failed to render");
    }
    const quiet = console.error;
    console.error = () => {};
    try {
      const view = await render(
        <main>
          <p data-testid="rest">the rest of the page</p>
          <AutonomyBoundary>
            <Broken />
          </AutonomyBoundary>
        </main>,
      );
      const note = $(view.host, "marvel0-autonomy-load-failed");
      expect(text(note)).toBe(E["marvel0.autonomy.load_failed"]);
      expect(text(note)).toContain("scripts/aeri_page_stop.py");
      expect(text($(view.host, "rest"))).toBe("the rest of the page");
    } finally {
      console.error = quiet;
    }
  });
});
