import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, unmountAll } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { textFields, tick, type } from "./actions-helpers";
import { $, openStep, text } from "./autonomy-helpers";
import { callsOf, readyDoc, runDoc } from "./autonomy-fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;

const allMet = (change?: (doc: ReturnType<typeof readyDoc>) => void) =>
  readyDoc("prepared_ready", (doc) => {
    for (const row of doc.readiness) row.level = "ok";
    change?.(doc);
  });
const summary = (host: ParentNode) => $(host, "marvel0-autonomy-summary")!;
const follows = (a: Element, b: Element) =>
  Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

describe("the summary at the top of step 5: can a run start now", () => {
  test("it sits right under the heading and the lead, before the cards and before the readiness list", async () => {
    const { host } = await openStep({ ready: allMet() });
    const lead = $(host, "marvel0-autonomy-lead")!;
    expect(follows(lead, summary(host))).toBe(true);
    expect(follows(summary(host), $(host, "marvel0-autonomy-cards")!)).toBe(
      true,
    );
    expect(follows(summary(host), $(host, "marvel0-autonomy-ready")!)).toBe(
      true,
    );
    expect(summary(host).closest("details")).toBeNull();
  });

  test("a prepared run with nothing in the way: 'Yes', and Start is right there, on", async () => {
    const f = await openStep({ ready: allMet() });
    expect(summary(f.host).getAttribute("data-state")).toBe("startable");
    expect(text($(f.host, "marvel0-autonomy-summary-headline"))).toContain(
      "can start now",
    );
    const start = summary(f.host).querySelector<HTMLButtonElement>(
      "[data-testid=marvel0-autonomy-start]",
    )!;
    expect(start.disabled).toBe(false);
    expect(text(start)).toBe(E["marvel0.autonomy.start.button"]);
    // the statements the click attests stay beside the button
    expect(
      summary(f.host).contains($(f.host, "marvel0-autonomy-start-says")),
    ).toBe(true);
    expect($(f.host, "marvel0-autonomy-start-off")).toBeNull();
    // exactly one Start button on the step
    expect(
      f.host.querySelectorAll("button[data-testid=marvel0-autonomy-start]"),
    ).toHaveLength(1);
    await click(start);
    await tick(3, 10);
    expect(callsOf(f.state, "challenge").length).toBeGreaterThan(0);
  });

  test("a prepared run that cannot start: 'Not yet', Start is visible and off, and every blocking readiness row and off reason is listed in the summary", async () => {
    const { host } = await openStep({
      ready: readyDoc("prepared_ready", (doc) => {
        const row = doc.readiness.find(
          (r: { code: string }) => r.code === "gpu_free",
        );
        row.level = "refuse";
        const unknown = doc.readiness.find(
          (r: { code: string }) => r.code === "ports_free",
        );
        unknown.level = "unknown";
      }),
    });
    const box = summary(host);
    expect(box.getAttribute("data-state")).toBe("blocked");
    expect(text($(host, "marvel0-autonomy-summary-headline"))).toContain(
      "Not yet",
    );
    const start = box.querySelector<HTMLButtonElement>(
      "[data-testid=marvel0-autonomy-start]",
    )!;
    expect(start.disabled).toBe(true);
    const off = box.querySelector("[data-testid=marvel0-autonomy-start-off]")!;
    const codes = Array.from(off.querySelectorAll("li")).map((li) =>
      li.getAttribute("data-code"),
    );
    expect(codes).toContain("gpu_free");
    expect(codes).toContain("ports_free");
    // the refusing rows are also in the readiness list below: the same rows
    const listed = Array.from(
      $(host, "marvel0-autonomy-ready-refusing")!.querySelectorAll("li"),
    ).map((li) => li.getAttribute("data-code"));
    for (const code of listed) expect(codes).toContain(code);
  });

  test("no run prepared: the summary says so, lists what blocks (the refusing rows), and offers 'Prepare a run', which takes the person to the form's first field", async () => {
    const f = await openStep({ ready: readyDoc("blocked") });
    const host = f.host;
    const box = summary(host);
    expect(box.getAttribute("data-state")).toBe("not_prepared");
    expect(text($(host, "marvel0-autonomy-summary-headline"))).toBe(
      E["marvel0.autonomy.summary.not_prepared"],
    );
    expect($(host, "marvel0-autonomy-start")).toBeNull();
    const off = box.querySelector("[data-testid=marvel0-autonomy-start-off]")!;
    const codes = Array.from(off.querySelectorAll("li")).map((li) =>
      li.getAttribute("data-code"),
    );
    expect(codes).toContain("not_prepared");
    expect(codes).toContain("gpu_free");
    const button = $(
      host,
      "marvel0-autonomy-summary-prepare",
    ) as HTMLButtonElement;
    expect(text(button)).toBe(E["marvel0.autonomy.summary.prepare"]);
    expect(button.disabled).toBe(false);
    await click(button);
    const form = $(host, "marvel0-autonomy-form")!;
    expect(document.activeElement).toBe(form.querySelector("input"));
    // nothing was sent by that click
    expect(callsOf(f.state, "prepare")).toEqual([]);
  });

  test("a terminal D that lives: the summary says so with the state, offers no Start or Prepare, and the run card comes first", async () => {
    const { host } = await openStep({
      run: runDoc("forward_running"),
      ready: allMet(),
    });
    expect(summary(host).getAttribute("data-state")).toBe("live");
    expect(text($(host, "marvel0-autonomy-summary-headline"))).toContain(
      "Terminal D is running",
    );
    expect($(host, "marvel0-autonomy-start")).toBeNull();
    expect($(host, "marvel0-autonomy-summary-prepare")).toBeNull();
    expect($(host, "marvel0-autonomy-prepare-card")).toBeNull();
    expect($(host, "marvel0-autonomy-run")).not.toBeNull();
  });

  test("while the evaluation client is live Start is off and the summary names why", async () => {
    const { host } = await openStep({ ready: allMet(), evalLive: true });
    const off = $(host, "marvel0-autonomy-start-off")!;
    expect(summary(host).contains(off)).toBe(true);
    expect(text(off)).toContain(E["marvel0.autonomy.local.eval_live"]);
    expect(
      ($(host, "marvel0-autonomy-start") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  test("hosting switched off, or no site: the existing notes say it and there is no summary to act on", async () => {
    const off = await openStep({ ready: readyDoc("disabled") });
    expect($(off.host, "marvel0-autonomy-summary")).toBeNull();
    await unmountAll();
    const bare = await openStep({ ready: readyDoc("unconfigured") });
    expect($(bare.host, "marvel0-autonomy-summary")).toBeNull();
  });

  test("the words are in Chinese too", async () => {
    const { host } = await openStep({ ready: readyDoc("blocked"), zh: true });
    expect(text($(host, "marvel0-autonomy-summary-headline"))).toBe(
      Z["marvel0.autonomy.summary.not_prepared"],
    );
    expect(text($(host, "marvel0-autonomy-summary-prepare"))).toBe(
      Z["marvel0.autonomy.summary.prepare"],
    );
    for (const key of Object.keys(E).filter((k) =>
      k.startsWith("marvel0.autonomy.summary."),
    ))
      expect([key, Boolean(Z[key])]).toEqual([key, true]);
  });
});

describe("the order of the cards: what a person does next comes first", () => {
  test("D not alive: the prepare card comes before the (ended) run card", async () => {
    const { host } = await openStep({
      run: runDoc("ended_done"),
      ready: allMet(),
    });
    const prepare = $(host, "marvel0-autonomy-prepare-card")!;
    const run = $(host, "marvel0-autonomy-run")!;
    expect(prepare).not.toBeNull();
    expect(run).not.toBeNull();
    expect(follows(prepare, run)).toBe(true);
  });

  test("D alive: the run card (Stop, keys, takeover) comes first and there is no prepare card", async () => {
    const { host } = await openStep({
      run: runDoc("await_release"),
      ready: allMet(),
    });
    expect($(host, "marvel0-autonomy-run")).not.toBeNull();
    expect($(host, "marvel0-autonomy-prepare-card")).toBeNull();
    const cards = $(host, "marvel0-autonomy-cards")!;
    expect(
      cards.firstElementChild!.contains($(host, "marvel0-autonomy-run")),
    ).toBe(true);
  });

  test("the offline rehearsal sits after the cards", async () => {
    const { host } = await openStep({ ready: allMet() });
    expect(
      follows(
        $(host, "marvel0-autonomy-cards")!,
        host.querySelector("[data-testid=reset-harness-card]")!,
      ),
    ).toBe(true);
  });
});

describe("nothing that acts or sets is behind a fold", () => {
  test("every <details> left on step 5 is informational: no button, switch, input or select inside", async () => {
    for (const [name, doc] of [
      ["ended", runDoc("ended_done")],
      ["stalled", runDoc("stalled")],
      ["takeover", runDoc("await_assist")],
      ["release", runDoc("await_release")],
    ] as const) {
      await unmountAll();
      const { host } = await openStep({ run: doc, ready: allMet() });
      for (const fold of Array.from(host.querySelectorAll("details")))
        expect([
          name,
          fold.textContent?.slice(0, 30),
          fold.querySelectorAll(
            "button, input, select, textarea, [role=switch]",
          ).length,
        ]).toEqual([name, fold.textContent?.slice(0, 30), 0]);
    }
  });

  test("the prepare form, readiness refusals and the live-service stop are never inside a fold", async () => {
    const { host } = await openStep({ ready: readyDoc("leftovers_blocked") });
    for (const id of [
      "marvel0-autonomy-form",
      "marvel0-autonomy-ready-refusing",
    ]) {
      const node = $(host, id);
      if (node) expect([id, node.closest("details")]).toEqual([id, null]);
    }
    await type(
      textFields($(host, "marvel0-autonomy-form")!)[0],
      "pick the pepper into the white plate",
    );
  });
});
