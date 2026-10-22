import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  AutonomyOverview,
  AutonomyPlan,
  loopPhase,
  LOOP_PHASES,
} from "../autonomy-overview";
import { runView, type StateId } from "../autonomy-doc";
import { runDoc } from "./autonomy-fixtures";
import { RUN_FRESH_MS } from "../autonomy-logic";
import { WorkstationNav } from "../workstation-nav";
import { LocaleProvider } from "@/components/levi-locale";

setupDom();
const known = new Set(Object.keys(en));
const view = (state: StateId = "forward_running") => {
  const run = runView(runDoc("forward_running"), known)!;
  run.state.id = state;
  return run;
};

describe("the operational overview", () => {
  test("shows only explicit phases, including repeated release and reverse", () => {
    expect(loopPhase(view("await_inventory"), true)).toBe("inventory");
    expect(loopPhase(view("await_release"), true)).toBe("release");
    expect(loopPhase(view(), true)).toBe("forward");
    expect(loopPhase(view("reverse_running"), true)).toBe("reverse");
    expect(loopPhase(view("await_home_p"), true)).toBe("prepare");
    for (const id of [
      "busy",
      "closing",
      "stopping",
      "fault_locked",
      "unknown",
      "await_resume",
      "orphaned",
    ] as const) {
      expect(loopPhase(view(id), true)).toBeNull();
    }
  });

  test("stale, foreign and inconsistent process facts never highlight a phase", () => {
    const run = view();
    expect(loopPhase(run, false)).toBeNull();
    run.state.foreign = true;
    expect(loopPhase(run, true)).toBeNull();
    run.state.foreign = false;
    run.d.alive = false;
    expect(loopPhase(run, true)).toBeNull();
    run.state.id = "ended";
    expect(loopPhase(run, true)).toBe("ended");
    run.d.alive = true;
    expect(loopPhase(run, true)).toBeNull();
  });

  test("unknown counters stay unknown; completed is not rounds released", async () => {
    const run = view();
    run.task = "Move the egg";
    run.progress = {
      round: 3,
      rounds: 5,
      released: 3,
      done: 1,
      discarded: 1,
      step: null,
      maxSteps: null,
      verdictLatched: false,
    };
    const { host, rerender } = await render(
      <AutonomyOverview run={run} current lastOk={Date.now()} />,
    );
    expect(host.textContent).toContain("Move the egg");
    const count = (id: string) =>
      host.querySelector(`[data-testid="marvel0-autonomy-overview-${id}"]`)
        ?.textContent;
    expect(count("done")).toBe("1");
    expect(count("step")).toBe("— / —");
    expect(host.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
    expect(host.querySelector("button")).toBeNull();
    expect(host.querySelector("a")).toBeNull();
    await rerender(
      <AutonomyOverview run={run} current={false} lastOk={Date.now()} />,
    );
    expect(host.querySelector('[aria-current="step"]')).toBeNull();
    expect(host.textContent).toContain(en["marvel0.autonomy.overview.stale"]);
  });

  test("a hung poll expires the phase even without any allowed keys", async () => {
    const run = view();
    run.keys = [];
    const { host } = await render(
      <AutonomyOverview
        run={run}
        current
        lastOk={Date.now() - RUN_FRESH_MS + 100}
      />,
    );
    expect(host.querySelector('[aria-current="step"]')).not.toBeNull();
    await waitFor(() => host.querySelector('[data-current="false"]'), {
      timeoutMs: 1500,
      label: "overview expiry while poll is hung",
    });
    expect(host.querySelector('[aria-current="step"]')).toBeNull();
    expect(host.textContent).toContain(en["marvel0.autonomy.overview.stale"]);
  });

  test("every phase has both languages and Chinese renders", async () => {
    for (const id of LOOP_PHASES) {
      const key = `marvel0.autonomy.overview.phase.${id}`;
      expect((en as Record<string, string>)[key]).toBeTruthy();
      expect((zh as Record<string, string>)[key]).toBeTruthy();
    }
    localStorage.setItem("levi-language", "zh");
    const { host } = await render(
      <LocaleProvider>
        <AutonomyOverview run={view()} current lastOk={Date.now()} />
      </LocaleProvider>,
    );
    expect(host.textContent).toContain(zh["marvel0.autonomy.overview.title"]);
    localStorage.clear();
  });

  test("the static plan explains every stage in both languages without run facts", async () => {
    for (const [language, catalog] of [
      ["en", en],
      ["zh", zh],
    ] as const) {
      localStorage.setItem("levi-language", language);
      const { host } = await render(
        <LocaleProvider>
          <AutonomyPlan />
        </LocaleProvider>,
      );
      expect(host.textContent).toContain(
        catalog["marvel0.autonomy.plan.title"],
      );
      expect(host.textContent).toContain(catalog["marvel0.autonomy.plan.note"]);
      expect(host.textContent).toContain(
        catalog["marvel0.autonomy.plan.ended"],
      );
      expect(host.querySelectorAll("ol li")).toHaveLength(LOOP_PHASES.length);
      expect(host.querySelector("[aria-current], dl, button, a")).toBeNull();
      expect(host.textContent).not.toContain(
        catalog["marvel0.autonomy.overview.phase.ended"],
      );
    }
    localStorage.clear();
  });

  test("navigation only links to the five existing step anchors", async () => {
    const { host } = await render(<WorkstationNav />);
    expect(
      Array.from(host.querySelectorAll("a"), (a) => a.getAttribute("href")),
    ).toEqual([
      "#marvel0-step-arm",
      "#marvel0-step-policy",
      "#marvel0-step-client",
      "#marvel0-step-run",
      "#marvel0-step-autonomy",
    ]);
    expect(host.querySelector("button")).toBeNull();
  });
});
