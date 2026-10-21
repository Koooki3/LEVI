import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { readModels, readSite } from "../catalog-logic";
import { CommandPanel } from "../command-panel";
import { readDeploy } from "../deploy-logic";
import { runView } from "../run-logic";
import { NO_CHOICE, effectiveSelection } from "../selection";
import type { RunActions } from "../use-run-actions";
import type { Marvel0Catalog } from "../use-marvel0-actions";
import {
  button,
  choose,
  fakeApi,
  field,
  ok,
  refused,
  tick,
  type,
  type Canned,
} from "./actions-helpers";
import { modelsAnswer, siteAnswer } from "./catalog-fixtures";
import { preview, siteWithRate } from "./command-fixtures";
import { runDoc } from "./ctl-fixtures";
import { idle } from "./deploy-fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const catalog = (site: Record<string, unknown>): Marvel0Catalog => ({
  loaded: true,
  models: readModels(modelsAnswer()),
  site: readSite(site),
  modelsProblem: null,
  siteProblem: null,
  reload: () => {},
});

interface Started {
  form: Record<string, unknown>;
  phrase: string;
}

async function open(
  options: {
    site?: Record<string, unknown>;
    canned?: Canned;
    zh?: boolean;
    rtc?: boolean;
    withRun?: boolean;
  } = {},
) {
  if (options.zh) localStorage.setItem("levi-language", "zh");
  const { api, calls } = fakeApi(options.canned ?? { preview: ok(preview()) });
  const status = readDeploy(idle());
  const models = readModels(modelsAnswer());
  const selection = {
    ...effectiveSelection(models, NO_CHOICE, status)!,
    ...(options.rtc === false ? { rtc: false } : {}),
  };
  const started: Started[] = [];
  const runActions: RunActions = {
    working: null,
    holding: false,
    refused: null,
    press: async () => true,
    hold: async () => true,
    end: async () => true,
    start: async (form, phrase) => {
      started.push({ form, phrase });
      return true;
    },
    clear: () => {},
  };
  const run = options.withRun
    ? runView(runDoc("idle_ready"), new Set(Object.keys(en)))
    : null;
  const card = (
    <CommandPanel
      api={api}
      catalog={catalog(options.site ?? siteWithRate())}
      status={status}
      selection={selection}
      onUseAnother={() => {}}
      run={run}
      runActions={options.withRun ? runActions : undefined}
    />
  );
  const view = await render(
    options.zh ? <LocaleProvider>{card}</LocaleProvider> : card,
  );
  return { ...view, calls, started };
}

const panel = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-command]") as HTMLElement;
const select = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-command-hz]") as HTMLSelectElement;
const facts = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-command-rate-facts]") as HTMLElement;
const note = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-command-rate-note]");
const text = (node: Element | null) => node?.textContent ?? "";
const fact = (host: HTMLElement, name: string) =>
  text(facts(host).querySelector(`[data-fact=${name}]`));
const make = async (host: HTMLElement) => {
  await click(button(panel(host), "Make the command")!);
  await tick();
};

describe("the control rate field", () => {
  test("a select of exactly 5, 10, 20, 25 and 50 Hz, on the default one, 10", async () => {
    const { host } = await open();
    const hz = select(host);
    expect(hz.tagName).toBe("SELECT");
    expect(Array.from(hz.options).map((o) => o.value)).toEqual([
      "5",
      "10",
      "20",
      "25",
      "50",
    ]);
    expect(Array.from(hz.options).map((o) => o.textContent)).toEqual([
      "5 Hz",
      "10 Hz (default)",
      "20 Hz",
      "25 Hz",
      "50 Hz",
    ]);
    expect(hz.value).toBe("10");
    expect(field(panel(host), "Control rate")).toBe(hz);
    expect(text(panel(host))).toContain(en["marvel0.hz.hint"]);
  });

  test("at the default rate the form asks as it always did: no rate in the request, no note, no hint about caps", async () => {
    const { host, calls } = await open();
    expect(facts(host).getAttribute("data-hz")).toBe("10");
    expect(facts(host).getAttribute("data-changed")).toBeNull();
    expect(note(host)).toBeNull();
    expect(text(facts(host))).not.toContain(en["marvel0.hz.facts.hint"]);
    await make(host);
    expect(calls.preview).toEqual([
      {
        model_id: "pi05_fr3_all_step49999",
        prompt: "pick the eggplant in the blue plate",
        max_steps: 500,
        eval_num: 5,
        min_z: 0.025,
        rtc: true,
      },
    ]);
  });

  test("every derived value is shown at 10 Hz, as the baseline", async () => {
    const { host } = await open();
    expect(fact(host, "period")).toContain("every 100 ms");
    expect(fact(host, "episode")).toContain("500 steps, at least 50 s");
    expect(fact(host, "step_caps")).toContain("0.1 m and 0.35 rad per step");
    expect(fact(host, "speed_caps")).toContain("up to 1 m/s and 3.5 rad/s");
    expect(fact(host, "chunk")).toContain("10 actions, 1 s");
    expect(fact(host, "horizon")).toContain("6 of 10 actions, 0.6 s");
    expect(fact(host, "beats")).toContain("about 3 beats of 100 ms (0.3 s)");
    expect(fact(host, "rtc")).toContain(
      "horizon 6 + 3 beats = 9 of 10 actions",
    );
    expect(fact(host, "rtc")).not.toContain("over");
  });

  test("choosing 20 Hz shows what changes at once: shorter episode, scaled caps, the same speed, more beats, a lower empty horizon, and the first-use note", async () => {
    const { host } = await open();
    await choose(select(host), "20");
    expect(facts(host).getAttribute("data-hz")).toBe("20");
    expect(facts(host).getAttribute("data-changed")).toBe("true");
    expect(fact(host, "period")).toContain("every 50 ms");
    expect(fact(host, "episode")).toContain("500 steps, at least 25 s");
    expect(fact(host, "step_caps")).toContain("0.05 m and 0.175 rad per step");
    expect(fact(host, "speed_caps")).toContain("up to 1 m/s and 3.5 rad/s");
    expect(fact(host, "chunk")).toContain("10 actions, 0.5 s");
    expect(fact(host, "horizon")).toContain("4 of 10 actions, 0.2 s");
    expect(fact(host, "beats")).toContain("about 6 beats of 50 ms (0.3 s)");
    expect(fact(host, "rtc")).toContain(
      "horizon 4 + 6 beats = 10 of 10 actions",
    );
    expect(text(facts(host))).toContain(en["marvel0.hz.facts.hint"]);
    expect(text(note(host))).toContain(
      "20 Hz is not the rate the policy was trained at (10 Hz)",
    );
    expect(text(note(host))).toContain("owner present");
    expect(text(note(host))).toContain("physical e-stop");
    const horizon = field(
      panel(host),
      "RTC execute horizon",
    ) as HTMLInputElement;
    expect(horizon.placeholder).toBe("4");
    expect(text(panel(host))).toContain(
      "At this rate an empty field uses 4, lowered from 6",
    );
  });

  test("at 5 Hz the caps stay, the speed they allow halves, and nothing is lowered", async () => {
    const { host } = await open();
    await choose(select(host), "5");
    expect(fact(host, "step_caps")).toContain("0.1 m and 0.35 rad per step");
    expect(fact(host, "speed_caps")).toContain("up to 0.5 m/s and 1.75 rad/s");
    expect(fact(host, "episode")).toContain("at least 100 s");
    expect(fact(host, "beats")).toContain("about 2 beats of 200 ms");
    expect(
      (field(panel(host), "RTC execute horizon") as HTMLInputElement)
        .placeholder,
    ).toBe("6");
    expect(text(panel(host))).not.toContain("lowered from");
    expect(note(host)).not.toBeNull();
  });

  test("at 50 Hz the RTC margin row says by how much the chunk is overrun", async () => {
    const { host } = await open();
    await choose(select(host), "50");
    expect(fact(host, "rtc")).toContain(
      "horizon 1 + 15 beats = 16 of 10 actions: 6 over, the client may wait for chunks",
    );
    expect(fact(host, "step_caps")).toContain("0.02 m and 0.07 rad per step");
  });

  test("the facts follow the typed steps and horizon, and RTC off drops the RTC rows", async () => {
    const { host, calls } = await open();
    await choose(select(host), "25");
    await type(field(panel(host), "Max steps per episode"), "1000");
    await type(field(panel(host), "RTC execute horizon"), "5");
    expect(fact(host, "episode")).toContain("1000 steps, at least 40 s");
    expect(fact(host, "horizon")).toContain("5 of 10 actions, 0.2 s");
    expect(fact(host, "rtc")).toContain(
      "horizon 5 + 8 beats = 13 of 10 actions: 3 over",
    );
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      control_hz: 25,
      max_steps: 1000,
      rtc_horizon: 5,
    });
    const off = await open({ rtc: false });
    await choose(select(off.host), "20");
    expect(facts(off.host).querySelector("[data-fact=rtc]")).toBeNull();
    expect(facts(off.host).querySelector("[data-fact=beats]")).toBeNull();
    expect(fact(off.host, "horizon")).toContain("6 of 10 actions, 0.3 s");
  });

  test("the request names the rate when it is not 10, and not when the person goes back to 10", async () => {
    const { host, calls } = await open();
    await choose(select(host), "20");
    await make(host);
    expect(calls.preview[0]).toMatchObject({ control_hz: 20, rtc: true });
    await choose(select(host), "10");
    await make(host);
    expect(calls.preview[1]).not.toHaveProperty("control_hz");
    await choose(select(host), "5");
    await make(host);
    expect(calls.preview[2]).toMatchObject({ control_hz: 5 });
  });

  test("a command made at one rate is stale when the rate changes", async () => {
    const { host } = await open();
    await make(host);
    const result = () =>
      host.querySelector("[data-testid=marvel0-command-result]")!;
    expect(result().getAttribute("data-stale")).toBeNull();
    await choose(select(host), "25");
    expect(result().getAttribute("data-stale")).toBe("true");
  });

  test("the warnings of the preview that name the rate are shown in the page's words", async () => {
    const warnings = [
      {
        code: "control_hz_not_trained",
        text: "English",
        params: { hz: 25, speed: "2.5" },
      },
      { code: "control_hz_untested", text: "English", params: { hz: 25 } },
      {
        code: "episode_short",
        text: "English",
        params: { steps: 500, hz: 25, seconds: 20, minimum: 30 },
      },
    ];
    const { host } = await open({
      canned: { preview: ok(preview({ warnings })) },
    });
    await choose(select(host), "25");
    await make(host);
    const shown = text(
      host.querySelector("[data-testid=marvel0-command-warnings]"),
    );
    expect(shown).toContain("2.5 times that speed");
    expect(shown).toContain(
      "A control rate of 25 Hz changes the arm's speed profile",
    );
    expect(shown).toContain("500 steps at 25 Hz last at least 20 s");
    expect(shown).not.toContain("English");
  });

  test("the server's refusal of the rate shows on the rate field in the page's words", async () => {
    const { host } = await open({
      canned: {
        preview: refused(
          422,
          "control_hz_unsupported",
          { field: "control_hz", options: [5, 10, 20, 25, 50] },
          "English control sentence",
        ),
      },
    });
    await choose(select(host), "20");
    await make(host);
    const error = select(host)
      .closest(".ds-field")
      ?.querySelector(".ds-field__error")?.textContent;
    expect(error).toBe(
      "This control rate is not offered. Choose one of 5, 10, 20, 25, 50 Hz.",
    );
    expect(text(panel(host))).not.toContain("English control sentence");
  });

  test("a site that does not offer rates has no rate field, and the form asks as before", async () => {
    const { host, calls } = await open({ site: siteAnswer() });
    expect(select(host)).toBeNull();
    expect(facts(host)).toBeNull();
    expect(note(host)).toBeNull();
    expect(panel(host).querySelectorAll("select").length).toBe(0);
    await make(host);
    expect(calls.preview[0]).not.toHaveProperty("control_hz");
  });
});

describe("the control rate in Chinese", () => {
  test("the field, the facts, the note and the lowered default are in Chinese, with no key left bare", async () => {
    const { host } = await open({ zh: true });
    await choose(select(host), "20");
    const shown = text(panel(host));
    for (const key of [
      "marvel0.hz.label",
      "marvel0.hz.hint",
      "marvel0.hz.facts.title",
      "marvel0.hz.facts.hint",
      "marvel0.hz.fact.episode",
      "marvel0.hz.fact.speed_caps",
      "marvel0.hz.fact.rtc",
    ])
      expect(shown).toContain(zh[key as keyof typeof zh]);
    expect(Array.from(select(host).options).map((o) => o.textContent)).toEqual([
      "5 Hz",
      "10 Hz（默认）",
      "20 Hz",
      "25 Hz",
      "50 Hz",
    ]);
    expect(fact(host, "episode")).toBe("片段时长500 步，至少 25 秒");
    expect(text(note(host))).toContain("20 Hz 不是策略训练时的频率（10 Hz）");
    expect(text(note(host))).toContain("机主在场");
    expect(shown).not.toMatch(/marvel0\.hz\./);
    expect(shown).not.toMatch(/\{\w+\}/);
  });
});

describe("starting the client at a chosen rate", () => {
  async function startDialog(hz: string) {
    const view = await open({ withRun: true });
    if (hz !== "10") await choose(select(view.host), hz);
    await make(view.host);
    await click(button(view.host, "Start the client")!);
    const dialog = view.host.ownerDocument.querySelector(
      "[data-testid=marvel0-run-start-dialog]",
    ) as HTMLElement;
    return { ...view, dialog };
  }

  test("the very form of the command, with the rate, goes to the start", async () => {
    const { dialog, started } = await startDialog("20");
    expect(text(dialog)).toContain("Control rate");
    expect(
      text(dialog.querySelector("[data-testid=marvel0-run-start-hz]")),
    ).toContain("20 Hz");
    expect(text(dialog)).toContain(en["marvel0.hz.start.note"]);
    for (const box of dialog.querySelectorAll("input[type=checkbox]"))
      await click(box);
    await type(
      dialog.querySelector("input:not([type=checkbox])"),
      "start client pi05_fr3_all_step49999_rtc_demo",
    );
    const go = Array.from(
      dialog.ownerDocument.querySelectorAll("[role=dialog] button"),
    ).find((b) => b.textContent?.trim() === "Start the client") as
      | HTMLButtonElement
      | undefined;
    await click(go!);
    await tick(3, 10);
    expect(started).toHaveLength(1);
    expect(started[0].form).toMatchObject({ control_hz: 20, rtc: true });
  });

  test("at 10 Hz the dialog shows the rate and no extra note, and the form has no rate", async () => {
    const { dialog } = await startDialog("10");
    expect(
      text(dialog.querySelector("[data-testid=marvel0-run-start-hz]")),
    ).toContain("10 Hz");
    expect(text(dialog)).not.toContain(en["marvel0.hz.start.note"]);
  });
});
