import {
  click,
  fire,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { readModels, readSite } from "../catalog-logic";
import { CommandPanel } from "../command-panel";
import { readDeploy } from "../deploy-logic";
import { NO_CHOICE, effectiveSelection, type Effective } from "../selection";
import type { ArmView } from "../stack-act-logic";
import type { Marvel0Catalog } from "../use-marvel0-actions";
import { modelsAnswer, siteAnswer, model, cfgModel } from "./catalog-fixtures";
import { COMMAND, preview, siteWithRate } from "./command-fixtures";
import { deployInfo, idle, ready, starting } from "./deploy-fixtures";
import {
  button,
  choose as pick,
  fakeApi,
  field,
  nothing,
  ok,
  refused,
  tick,
  type,
  type Canned,
} from "./actions-helpers";

setupDom();

const SFT = "pi05_fr3_all_step49999";
const CFG = "recap_cfg_r2_best_step14300_jax";

const catalog = (over: Partial<Marvel0Catalog> = {}): Marvel0Catalog => ({
  loaded: true,
  models: readModels(modelsAnswer()),
  site: readSite(siteAnswer()),
  modelsProblem: null,
  siteProblem: null,
  reload: () => {},
  ...over,
});

let clipboard: string[] = [];
const realClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
afterEach(() => {
  clipboard = [];
  if (realClipboard)
    Object.defineProperty(navigator, "clipboard", realClipboard);
  else delete (navigator as unknown as Record<string, unknown>).clipboard;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});
function withClipboard() {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: {
      writeText: async (value: string) => {
        clipboard.push(value);
      },
    },
  });
}

/** The page's one selection as the page computes it, from the models, what
 * was picked in the policy server card (nothing) and the server's state; a
 * test may override parts to put the card in a state the page would not. */
function selectionFor(
  status: ReturnType<typeof readDeploy>,
  over: Partial<Effective> = {},
  models = readModels(modelsAnswer()),
): Effective {
  return {
    ...effectiveSelection(models, NO_CHOICE, status)!,
    ...over,
    ...(over.rtc !== undefined && over.inferenceMode === undefined
      ? { inferenceMode: over.rtc ? ("rtc" as const) : ("plain" as const) }
      : {}),
  };
}

async function open(
  canned: Canned = { preview: ok(preview()) },
  status: ReturnType<typeof readDeploy> = readDeploy(idle()),
  zhLanguage = false,
  over: Partial<Effective> = {},
  resumeBody: Record<string, unknown> | null = null,
  onCopy?: () => boolean,
  arm: ArmView | null = null,
) {
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const { api, calls } = fakeApi(canned);
  const used: string[] = [];
  let armNow = arm;
  const panel = (selection: Effective) => {
    const card = (
      <CommandPanel
        api={api}
        catalog={catalog()}
        status={status}
        selection={selection}
        onUseAnother={() => used.push("another")}
        resumeBody={resumeBody}
        onCopy={onCopy}
        arm={armNow}
      />
    );
    return zhLanguage ? <LocaleProvider>{card}</LocaleProvider> : card;
  };
  const view = await render(panel(selectionFor(status, over)));
  return {
    ...view,
    api,
    calls,
    used,
    /** The policy server card chose something else. */
    choose: (change: Partial<Effective>) =>
      view.rerender(panel(selectionFor(status, change))),
    /** The stack document says another arm controller. */
    setArm: (next: ArmView | null) => {
      armNow = next;
      return view.rerender(panel(selectionFor(status, over)));
    },
  };
}
const card = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-command]") as HTMLElement;
const text = (node: Element | null) => node?.textContent ?? "";
const make = async (host: HTMLElement) => {
  await click(button(card(host), "Make the command")!);
  await tick();
};

describe("the Client command card", () => {
  test("a resumed run pins every saved parameter and previews the same body with only the prompt changed", async () => {
    const saved = {
      model_id: SFT,
      prompt: "old task",
      max_steps: 500,
      eval_num: 4,
      min_z: 0.03,
      rtc: true,
      rtc_horizon: 7,
      rollout_group: "original_group",
      note: "original note",
      control_hz: 20,
    };
    const { host, calls } = await open(
      { preview: ok(preview()) },
      readDeploy(ready()),
      false,
      {},
      saved,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-command-resumed]"),
      {
        label: "the saved run form",
      },
    );
    const prompt = field(host, "Task instruction")!;
    const steps = field(host, "Max steps per episode (--max-timesteps)")!;
    expect((steps as HTMLInputElement).disabled).toBe(true);
    expect((prompt as HTMLInputElement).disabled).toBe(false);
    await type(prompt, "new task");
    await make(host);
    expect(calls.preview[0]).toEqual({ ...saved, prompt: "new task" });
  });

  test("exiting the saved-form mode keeps the changed prompt and lets the person edit other parameters", async () => {
    const saved = {
      model_id: SFT,
      prompt: "old task",
      max_steps: 500,
      eval_num: 4,
      min_z: 0.03,
      rtc: true,
      rtc_horizon: 7,
      rollout_group: "original_group",
      note: "original note",
      control_hz: 20,
    };
    const { host, calls } = await open(
      { preview: ok(preview()) },
      readDeploy(ready()),
      false,
      {},
      saved,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-command-resumed]"),
      {
        label: "the saved run form",
      },
    );
    const prompt = field(host, "Task instruction")!;
    await type(prompt, "new task");
    await click(button(host, en["marvel0.episodeChange.editAll"])!);
    const steps = field(host, "Max steps per episode (--max-timesteps)")!;
    expect((steps as HTMLInputElement).disabled).toBe(false);
    expect((prompt as HTMLInputElement).value).toBe("new task");
    await type(steps, "321");
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      ...saved,
      max_steps: 321,
      prompt: "new task",
    });
  });

  test("says what it writes, how to run it, and that the arm's settings come from the site file", async () => {
    const { host } = await open();
    expect(text(card(host))).toContain(en["marvel0.cmd.scope"]);
    // The card says where the client is started (here, after Make the
    // command), not that the Run card does it: that card hosts it afterwards.
    expect(en["marvel0.cmd.scope"]).toContain("start the client from here");
    expect(en["marvel0.cmd.scope"]).not.toContain("Run card");
    // The card no longer says the page never starts the client.
    expect(en["marvel0.cmd.scope"]).not.toContain("never");
    expect(en["marvel0.cmd.scope"]).toContain("site file");
  });

  test("opens on the site's defaults and the fields the server fills in are empty", async () => {
    const { host } = await open();
    const c = card(host);
    expect((field(c, "Task instruction") as HTMLInputElement).value).toBe(
      "pick the eggplant in the blue plate",
    );
    expect((field(c, "Max steps per episode") as HTMLInputElement).value).toBe(
      "500",
    );
    expect((field(c, "Eval number") as HTMLInputElement).value).toBe("5");
    expect((field(c, "Lowest height") as HTMLInputElement).value).toBe("0.025");
    const horizon = field(c, "RTC execute horizon") as HTMLInputElement;
    expect(horizon.value).toBe("");
    expect(horizon.placeholder).toBe("6");
    const group = field(c, "Rollout group") as HTMLInputElement;
    expect(group.value).toBe("");
    expect(group.placeholder).toBe(`${SFT}_rtc_demo`);
    expect((field(c, "Note") as HTMLInputElement).value).toBe("");
  });

  test("suggests the known task instructions", async () => {
    const { host } = await open();
    const options = Array.from(
      host.querySelectorAll("datalist#marvel0-known-prompts option"),
    ).map((option) => option.getAttribute("value"));
    expect(options).toContain("stack the plates of same color together");
    expect(options).toHaveLength(3);
  });

  test("a restricted model is offered only the two instructions it was trained on", async () => {
    const restricted = model({
      id: "pi05_fr3_sp_v1",
      config: "pi05_fr3_sp",
      restricted_prompts: true,
    });
    const models = readModels(modelsAnswer({ models: [restricted, model()] }));
    const { api } = fakeApi({ preview: ok(preview()) });
    const status = readDeploy(idle());
    const panel = (modelId: string) => (
      <CommandPanel
        api={api}
        catalog={catalog({ models })}
        status={status}
        selection={selectionFor(status, { modelId }, models)}
        onUseAnother={() => {}}
      />
    );
    const view = await render(panel("pi05_fr3_sp_v1"));
    const options = () =>
      Array.from(
        view.host.querySelectorAll("datalist#marvel0-known-prompts option"),
      ).map((option) => option.getAttribute("value"));
    expect(options()).toEqual([
      "pick screws of same size into the empty slot of screw box",
      "stack the plates of same color together",
    ]);
    // The policy server card chose another model: the list follows.
    await view.rerender(panel(SFT));
    expect(options()).toHaveLength(3);
  });

  test("lists the flags the site fixes, and offers no control to change them", async () => {
    const { host } = await open();
    const fixed = host.querySelector("[data-testid=marvel0-command-fixed]")!;
    expect(fixed.getAttribute("lang")).toBe("en");
    expect(text(fixed)).toContain("--max-xy=0.85");
    expect(text(fixed)).toContain("--require-matching-server");
    expect(text(fixed)).toContain("--no-allow-unknown-prompt");
    const labels = Array.from(card(host).querySelectorAll("label")).map((l) =>
      text(l),
    );
    for (const fixedFlag of [
      "max-xy",
      "max-step",
      "home",
      "binarize",
      "levi-mode",
      "camera",
      "rx-range",
      "allow-unknown",
    ])
      expect(labels.join(" ")).not.toContain(fixedFlag);
    // Only these controls: the form, nothing for the fixed flags, and no
    // model or RTC control of its own (they follow the policy server card).
    const inputs = Array.from(
      card(host).querySelectorAll("input, select"),
    ).length;
    // prompt, steps, eval, min z, horizon, in-chunk acceleration, group, note
    expect(inputs).toBe(8);
    expect(card(host).querySelectorAll("select").length).toBe(0);
    const switches = card(host).querySelectorAll("[role=switch]");
    expect(switches).toHaveLength(1);
    expect(
      switches[0].closest("[data-testid=marvel0-command-chunk-accel]"),
    ).not.toBeNull();
  });
});

describe("in-chunk acceleration", () => {
  const accel = (host: HTMLElement) =>
    card(host).querySelector<HTMLInputElement>(
      "[data-testid=marvel0-command-chunk-accel] input[role=switch]",
    )!;
  const why = (host: HTMLElement) =>
    card(host).querySelector("[data-testid=marvel0-command-chunk-accel-why]");

  test("with plain inference it is off by default, and on it is sent; off it is left out", async () => {
    const { host, calls } = await open(undefined, readDeploy(idle()), false, {
      rtc: false,
    });
    expect(text(card(host))).toContain(en["marvel0.cmd.field.chunk_accel"]);
    expect(text(card(host))).toContain(
      en["marvel0.cmd.field.chunk_accel_hint"],
    );
    expect(accel(host).checked).toBe(false);
    expect(accel(host).disabled).toBe(false);
    expect(why(host)).toBeNull();
    await make(host);
    expect(calls.preview[0]).not.toHaveProperty("chunk_accel");
    await click(accel(host));
    expect(accel(host).checked).toBe(true);
    await make(host);
    expect(calls.preview[1]).toMatchObject({
      inference_mode: "plain",
      rtc: false,
      chunk_accel: true,
    });
  });

  test("with RTC or VLASH it can be turned on too: rule v3 is the only rule there, and it is sent", async () => {
    for (const over of [
      { rtc: true, inferenceMode: "rtc" as const },
      { rtc: false, inferenceMode: "vlash" as const },
    ]) {
      const { host, calls } = await open(
        undefined,
        readDeploy(idle()),
        false,
        over,
      );
      expect(accel(host).disabled).toBe(false);
      expect(accel(host).checked).toBe(false);
      expect(why(host)).toBeNull();
      await make(host);
      expect(calls.preview[0]).not.toHaveProperty("chunk_accel");
      await click(accel(host));
      expect(accel(host).checked).toBe(true);
      // rules v1 and v2 run with plain inference only: they are shown, disabled, and v3 is chosen
      const radio = (value: string) =>
        card(host).querySelector<HTMLInputElement>(
          `[data-testid=marvel0-command-chunk-accel-rule-${value}]`,
        )!;
      expect(radio("v3").checked).toBe(true);
      expect(radio("v3").disabled).toBe(false);
      expect(radio("v1").disabled).toBe(true);
      expect(radio("v2").disabled).toBe(true);
      await make(host);
      expect(calls.preview[1]).toMatchObject({
        inference_mode: over.inferenceMode,
        rtc: over.rtc,
        chunk_accel: true,
        chunk_accel_rule: "v3",
        chunk_accel_alpha: 0.005,
      });
      expect(calls.preview[1]).not.toHaveProperty("chunk_accel_lambda");
    }
  });

  test("turned on with plain inference, then RTC chosen: it stays on as rule v3, and plain brings the old rule back", async () => {
    const { host, calls, choose } = await open(
      undefined,
      readDeploy(idle()),
      false,
      { rtc: false },
    );
    await click(accel(host));
    // v2 remembered from plain
    await click(
      card(host).querySelector<HTMLInputElement>(
        "[data-testid=marvel0-command-chunk-accel-rule-v2]",
      )!,
    );
    await choose({ rtc: true });
    expect(accel(host).checked).toBe(true);
    expect(accel(host).disabled).toBe(false);
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      inference_mode: "rtc",
      chunk_accel: true,
      chunk_accel_rule: "v3",
    });
    // back to plain: the choice made there is the form's again
    await choose({ rtc: false });
    expect(accel(host).checked).toBe(true);
    expect(
      card(host).querySelector<HTMLInputElement>(
        "[data-testid=marvel0-command-chunk-accel-rule-v2]",
      )!.checked,
    ).toBe(true);
    await make(host);
    expect(calls.preview[1]).toMatchObject({
      inference_mode: "plain",
      chunk_accel_rule: "v2",
    });
  });

  test("at another rate than 10 Hz it is off and disabled with the reason, and never sent", async () => {
    const { api, calls } = fakeApi({ preview: ok(preview()) });
    const status = readDeploy(idle());
    const { host } = await render(
      <CommandPanel
        api={api}
        catalog={catalog({ site: readSite(siteWithRate()) })}
        status={status}
        selection={selectionFor(status, { rtc: false })}
        onUseAnother={() => {}}
      />,
    );
    await click(accel(host));
    expect(accel(host).checked).toBe(true);
    // another rate turns the switch off (the accelerated clients run at 10 Hz only)
    await pick(
      host.querySelector<HTMLSelectElement>("[data-testid=marvel0-command-hz]"),
      "20",
    );
    expect(accel(host).checked).toBe(false);
    expect(accel(host).disabled).toBe(true);
    expect(text(why(host))).toBe(
      "Not available at 20 Hz: in-chunk acceleration runs only at the policy's 10 Hz.",
    );
    await make(host);
    expect(calls.preview[0]).not.toHaveProperty("chunk_accel");
    expect(calls.preview[0]).toMatchObject({ control_hz: 20 });
  });

  test("a changed switch marks the command on show as no longer matching", async () => {
    const { host } = await open(undefined, readDeploy(idle()), false, {
      rtc: false,
    });
    await make(host);
    const result = () =>
      host.querySelector("[data-testid=marvel0-command-result]") as HTMLElement;
    expect(result().getAttribute("data-stale")).toBeNull();
    await click(accel(host));
    expect(result().getAttribute("data-stale")).toBe("true");
    await click(accel(host));
    expect(result().getAttribute("data-stale")).toBeNull();
  });

  test("the server's refusal lands under the switch, in the page's words", async () => {
    const { host } = await open(
      {
        preview: refused(
          422,
          "chunk_accel_requires_plain",
          { field: "chunk_accel" },
          "English chunk_accel sentence",
        ),
      },
      readDeploy(idle()),
      false,
      { rtc: false },
    );
    await click(accel(host));
    await make(host);
    const box = card(host).querySelector(
      "[data-testid=marvel0-command-chunk-accel]",
    )!;
    expect(text(box.querySelector(".ds-field__error"))).toBe(
      en["marvel0.err.chunk_accel_requires_plain"],
    );
    expect(accel(host).getAttribute("aria-invalid")).toBe("true");
    expect(text(card(host))).not.toContain("English chunk_accel sentence");
    expect(
      host.querySelector("[data-testid=marvel0-command-problem]"),
    ).toBeNull();
  });

  test("the result says it is on, with the speed and lag the server answered", async () => {
    const { host } = await open(
      {
        preview: ok(
          preview({
            control: {
              hz: 10,
              chunk_accel: {
                enabled: true,
                experimental: true,
                speed_m_s: 0.25,
                tau_s: 0.2,
              },
            },
          }),
        ),
      },
      readDeploy(idle()),
      false,
      { rtc: false },
    );
    await click(accel(host));
    await make(host);
    const line = host.querySelector(
      "[data-testid=marvel0-command-chunk-accel-on]",
    );
    expect(text(line)).toContain("up to 0.25 m/s");
    expect(text(line)).toContain("0.2 s lag");
  });

  test("a resumed run with it on keeps it in the body and shows it, disabled", async () => {
    const saved = {
      model_id: SFT,
      prompt: "old task",
      max_steps: 500,
      eval_num: 4,
      min_z: 0.03,
      rtc: false,
      inference_mode: "plain",
      rtc_horizon: 7,
      rollout_group: "original_group",
      note: "original note",
      control_hz: 10,
      chunk_accel: true,
    };
    const { host, calls } = await open(
      { preview: ok(preview()) },
      readDeploy(ready()),
      false,
      { rtc: false },
      saved,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the saved run form" },
    );
    expect(accel(host).checked).toBe(true);
    expect(accel(host).disabled).toBe(true);
    await type(field(host, "Task instruction")!, "new task");
    await make(host);
    expect(calls.preview[0]).toEqual({ ...saved, prompt: "new task" });
    // Editing every parameter keeps the switch as the run had it.
    await click(button(host, en["marvel0.episodeChange.editAll"])!);
    expect(accel(host).checked).toBe(true);
    expect(accel(host).disabled).toBe(false);
    await make(host);
    expect(calls.preview[1]).toMatchObject({ chunk_accel: true });
  });

  test("a resumed v1 run edited under RTC is rule v3 there, and plain brings its v1 back", async () => {
    // final review 2026-10-31: with rules v1 and v2 only, RTC cleared the restored value and plain did not bring it
    // back; rule v3 runs with RTC, so the switch stays and v1 (plain only) waits behind it
    const saved = {
      model_id: SFT,
      prompt: "old task",
      max_steps: 500,
      eval_num: 4,
      min_z: 0.03,
      rtc: false,
      inference_mode: "plain",
      rtc_horizon: 7,
      rollout_group: "original_group",
      note: "original note",
      control_hz: 10,
      chunk_accel: true,
    };
    const { host, calls, choose } = await open(
      { preview: ok(preview()) },
      readDeploy(ready()),
      false,
      { rtc: true },
      saved,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the saved run form" },
    );
    await click(button(host, en["marvel0.episodeChange.editAll"])!);
    expect(accel(host).checked).toBe(true);
    await make(host);
    expect(calls.preview[calls.preview.length - 1]).toMatchObject({
      chunk_accel: true,
      chunk_accel_rule: "v3",
    });
    await choose({ rtc: false });
    expect(accel(host).disabled).toBe(false);
    expect(accel(host).checked).toBe(true);
    await make(host);
    const last = calls.preview[calls.preview.length - 1];
    expect(last).toMatchObject({ chunk_accel: true, inference_mode: "plain" });
    expect(last).not.toHaveProperty("chunk_accel_rule");
  });
});

describe("the rule of in-chunk acceleration", () => {
  const accel = (host: HTMLElement) =>
    card(host).querySelector<HTMLInputElement>(
      "[data-testid=marvel0-command-chunk-accel] input[role=switch]",
    )!;
  const rule = (host: HTMLElement, value: "v1" | "v2" | "v3") =>
    card(host).querySelector<HTMLInputElement>(
      `[data-testid=marvel0-command-chunk-accel-rule-${value}]`,
    );
  const lambda = (host: HTMLElement) =>
    card(host).querySelector<HTMLSelectElement>(
      "[data-testid=marvel0-command-chunk-accel-lambda]",
    );
  const alpha = (host: HTMLElement) =>
    card(host).querySelector<HTMLSelectElement>(
      "[data-testid=marvel0-command-chunk-accel-alpha]",
    );
  const box = (host: HTMLElement) =>
    card(host).querySelector("[data-testid=marvel0-command-chunk-accel]")!;
  const savedV2 = {
    model_id: SFT,
    prompt: "old task",
    max_steps: 500,
    eval_num: 4,
    min_z: 0.03,
    rtc: false,
    inference_mode: "plain",
    rtc_horizon: 7,
    rollout_group: "original_group",
    note: "original note",
    control_hz: 10,
    chunk_accel: true,
    chunk_accel_rule: "v2",
  };

  test("hidden while the switch is off; on, v3 is chosen; v1 sends nothing more, v2 and v3 send their rule", async () => {
    const { host, calls } = await open(undefined, readDeploy(idle()), false, {
      rtc: false,
    });
    expect(rule(host, "v1")).toBeNull();
    expect(rule(host, "v2")).toBeNull();
    expect(rule(host, "v3")).toBeNull();
    expect(lambda(host)).toBeNull();
    await click(accel(host));
    // the finished rule is on by default: v3, the phase clock
    expect(rule(host, "v3")!.checked).toBe(true);
    expect(rule(host, "v1")!.checked).toBe(false);
    expect(rule(host, "v2")!.checked).toBe(false);
    for (const value of ["v1", "v2", "v3"] as const)
      expect(rule(host, value)!.disabled).toBe(false);
    // three radio buttons of one group (arrow keys move between them), each with its words
    expect(rule(host, "v1")!.type).toBe("radio");
    expect(rule(host, "v1")!.name).toBe(rule(host, "v3")!.name);
    expect(rule(host, "v1")!.closest("fieldset")).not.toBeNull();
    for (const key of [
      "marvel0.cmd.field.chunk_accel_rule",
      "marvel0.cmd.field.chunk_accel_rule_v1",
      "marvel0.cmd.field.chunk_accel_rule_v1_hint",
      "marvel0.cmd.field.chunk_accel_rule_v2",
      "marvel0.cmd.field.chunk_accel_rule_v2_hint",
      "marvel0.cmd.field.chunk_accel_rule_v3",
      "marvel0.cmd.field.chunk_accel_rule_v3_hint",
    ] as const)
      expect(text(box(host)), key).toContain(en[key]);
    expect(card(host).querySelectorAll("[role=switch]")).toHaveLength(1);
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      chunk_accel: true,
      chunk_accel_rule: "v3",
      chunk_accel_alpha: 0.005,
    });
    expect(calls.preview[0]).not.toHaveProperty("chunk_accel_lambda");
    await click(rule(host, "v2")!);
    expect(rule(host, "v2")!.checked).toBe(true);
    expect(rule(host, "v3")!.checked).toBe(false);
    await make(host);
    expect(calls.preview[1]).toMatchObject({
      inference_mode: "plain",
      chunk_accel: true,
      chunk_accel_rule: "v2",
    });
    expect(calls.preview[1]).not.toHaveProperty("chunk_accel_alpha");
    await click(rule(host, "v1")!);
    await make(host);
    expect(calls.preview[2]).toMatchObject({ chunk_accel: true });
    expect(calls.preview[2]).not.toHaveProperty("chunk_accel_rule");
  });

  test("rule v3 shows the lambda and the alpha to choose, only while it is the rule", async () => {
    const { host, calls } = await open(undefined, readDeploy(idle()), false, {
      rtc: false,
    });
    await click(accel(host));
    // one more select each, in the box of the switch, with their words
    expect(card(host).querySelectorAll("select")).toHaveLength(2);
    expect(box(host).contains(lambda(host))).toBe(true);
    expect(box(host).contains(alpha(host))).toBe(true);
    for (const key of [
      "marvel0.cmd.field.chunk_accel_lambda",
      "marvel0.cmd.field.chunk_accel_lambda_hint",
      "marvel0.cmd.field.chunk_accel_alpha",
      "marvel0.cmd.field.chunk_accel_alpha_hint",
    ] as const)
      expect(text(box(host)), key).toContain(en[key]);
    // the lambda opens on "default" (the server's number), then the page's options
    expect(
      Array.from(lambda(host)!.options).map((o) => [o.value, o.text]),
    ).toEqual([
      ["", en["marvel0.cmd.field.chunk_accel_lambda_default"]],
      ["2.0", "2.0"],
      ["2.5", "2.5"],
      ["3.0", "3.0"],
    ]);
    expect(lambda(host)!.value).toBe("");
    // the alpha opens on the controller's default and says that the choice has to match set_clip.py --show
    expect(Array.from(alpha(host)!.options).map((o) => o.value)).toEqual([
      "0.005",
      "0.01",
    ]);
    expect(alpha(host)!.value).toBe("0.005");
    expect(text(alpha(host))).toContain("the controller's default");
    expect(en["marvel0.cmd.field.chunk_accel_alpha_hint"]).toContain(
      "set_clip.py --show",
    );
    expect(zh["marvel0.cmd.field.chunk_accel_alpha_hint"]).toContain(
      "set_clip.py --show",
    );
    await pick(lambda(host), "2.5");
    await pick(alpha(host), "0.01");
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      chunk_accel: true,
      chunk_accel_rule: "v3",
      chunk_accel_lambda: 2.5,
      chunk_accel_alpha: 0.01,
    });
    // choosing the empty lambda again sends none
    await pick(lambda(host), "");
    await make(host);
    expect(calls.preview[1]).not.toHaveProperty("chunk_accel_lambda");
    expect(calls.preview[1]).toMatchObject({ chunk_accel_alpha: 0.01 });
    // another rule hides them and sends neither; the choice is back when v3 is
    await click(rule(host, "v1")!);
    expect(lambda(host)).toBeNull();
    expect(alpha(host)).toBeNull();
    await make(host);
    expect(calls.preview[2]).not.toHaveProperty("chunk_accel_alpha");
    await click(rule(host, "v3")!);
    expect(alpha(host)!.value).toBe("0.01");
  });

  describe("with the arm controller that runs", () => {
    /** The facts of `act.arm` for an anyspeedup A (or none) at a preset. */
    const arm = (
      preset: string,
      over: Partial<ArmView> = {},
      running = true,
    ): ArmView => {
      const alpha = preset === "today" ? 0.005 : 0.01;
      const named = { controller: "anyspeedup" as const, preset };
      return {
        configured: named,
        enabled: true,
        running: running ? named : null,
        status: running ? "match" : "idle",
        alpha,
        problem: null,
        rampOk: preset === "today",
        suffix: `_any-${preset}`,
        ...over,
      };
    };
    const lab: ArmView = {
      configured: { controller: "lab", preset: null },
      enabled: true,
      running: { controller: "lab", preset: null },
      status: "match",
      alpha: null,
      problem: null,
      rampOk: true,
      suffix: "",
    };
    const openArm = (given: ArmView | null, zhLanguage = false) =>
      open(
        undefined,
        readDeploy(idle()),
        zhLanguage,
        { rtc: false },
        null,
        undefined,
        given,
      );

    test.each([
      ["S1", 0.01, "0.01"],
      ["today", 0.005, "0.005"],
    ] as const)(
      "preset %s: the alpha field shows %s, cannot be changed, says why, and the command is made with it",
      async (preset, alphaNumber, shown) => {
        const { host, calls } = await openArm(arm(preset));
        await click(accel(host));
        expect(alpha(host)!.value).toBe(shown);
        expect(alpha(host)!.disabled).toBe(true);
        expect(alpha(host)!.getAttribute("data-fixed-by")).toBe("anyspeedup");
        expect(text(box(host))).toContain(
          `anyspeedup, preset ${preset}): alpha ${shown}`,
        );
        // the anyspeedup controller's own read-back tool, not the lab's
        expect(text(box(host))).toContain("set_shaping.py --show");
        expect(text(box(host))).toContain("stop A, change the control file");
        expect(text(box(host))).not.toContain(
          en["marvel0.cmd.field.chunk_accel_alpha_hint"],
        );
        // the lambda stays the person's
        expect(lambda(host)!.disabled).toBe(false);
        await make(host);
        expect(calls.preview[0]).toMatchObject({
          chunk_accel: true,
          chunk_accel_rule: "v3",
          chunk_accel_alpha: alphaNumber,
        });
      },
    );

    test("an alpha chosen before the stack document arrived does not survive it: the body carries the preset's", async () => {
      const { host, calls, setArm } = await open(
        undefined,
        readDeploy(idle()),
        false,
        { rtc: false },
      );
      await click(accel(host));
      await pick(alpha(host), "0.005");
      expect(alpha(host)!.disabled).toBe(false);
      await make(host);
      expect(calls.preview[0]).toMatchObject({ chunk_accel_alpha: 0.005 });
      await setArm(arm("S1"));
      expect(alpha(host)!.value).toBe("0.01");
      expect(alpha(host)!.disabled).toBe(true);
      // the command on show was made for the other alpha: the form has outgrown it
      await make(host);
      expect(calls.preview[1]).toMatchObject({ chunk_accel_alpha: 0.01 });
      // and back: the person's own choice was kept, and is free again
      await setArm(null);
      expect(alpha(host)!.disabled).toBe(false);
      expect(alpha(host)!.value).toBe("0.005");
    });

    test("the lab's controller, an unknown one and a stack with no A running leave the field free", async () => {
      for (const given of [
        null,
        lab,
        // (the file names anyspeedup but the stack is off: the page starts nothing)
        arm(
          "S1",
          { enabled: false, alpha: null, suffix: "", rampOk: true },
          false,
        ),
      ]) {
        const { host, calls } = await openArm(given);
        await click(accel(host));
        expect(alpha(host)!.disabled, JSON.stringify(given)).toBe(false);
        expect(alpha(host)!.value).toBe("0.005");
        expect(alpha(host)!.hasAttribute("data-fixed-by")).toBe(false);
        expect(text(box(host))).toContain(
          en["marvel0.cmd.field.chunk_accel_alpha_hint"],
        );
        await pick(alpha(host), "0.01");
        await make(host);
        expect(calls.preview[0]).toMatchObject({ chunk_accel_alpha: 0.01 });
      }
    });

    test.each(["changed", "foreign", "unknown"] as const)(
      "an A that is not the file's (%s): nothing is forced and the card says rule v3 will be refused",
      async (problem) => {
        const { host, calls } = await openArm(
          arm("S1", {
            running: null,
            status: problem,
            problem,
            alpha: null,
            rampOk: false,
          }),
        );
        await click(accel(host));
        expect(alpha(host)!.disabled).toBe(false);
        const said = text(
          box(host).querySelector("[data-testid=marvel0-command-arm-problem]"),
        );
        expect(said).toBe(en[`marvel0.ctl.arm.problem_${problem}`]);
        await pick(alpha(host), "0.01");
        await make(host);
        expect(calls.preview[0]).toMatchObject({ chunk_accel_alpha: 0.01 });
      },
    );

    test("preset S1: rules v1 and v2 are not offered, rule v3 is on, and the words say why", async () => {
      const { host, calls } = await openArm(arm("S1"));
      await click(accel(host));
      expect(rule(host, "v3")!.checked).toBe(true);
      expect(rule(host, "v1")!.disabled).toBe(true);
      expect(rule(host, "v2")!.disabled).toBe(true);
      expect(rule(host, "v3")!.disabled).toBe(false);
      expect(
        text(
          box(host).querySelector("[data-testid=marvel0-command-ramp-locked]"),
        ),
      ).toContain("preset S1 only rule v3");
      // even a v2 the person had chosen before is not sent
      await make(host);
      expect(calls.preview[0]).toMatchObject({ chunk_accel_rule: "v3" });
    });

    test("preset today keeps the ramp rules, as the lab's controller does", async () => {
      for (const given of [arm("today"), lab, null]) {
        const { host } = await openArm(given);
        await click(accel(host));
        for (const value of ["v1", "v2", "v3"] as const)
          expect(rule(host, value)!.disabled, JSON.stringify(given)).toBe(
            false,
          );
        expect(
          box(host).querySelector("[data-testid=marvel0-command-ramp-locked]"),
        ).toBeNull();
      }
    });

    test("the default group's placeholder carries the controller's suffix", async () => {
      const group = (host: HTMLElement) =>
        Array.from(card(host).querySelectorAll("input")).find((input) =>
          input.placeholder.includes("_demo"),
        )!.placeholder;
      const plain = await openArm(lab);
      const tagged = await openArm(arm("S1"));
      expect(group(tagged.host)).toBe(`${group(plain.host)}_any-S1`);
    });

    test("the words are in both languages and hold no placeholder", async () => {
      expect(zh["marvel0.cmd.field.chunk_accel_alpha_arm"]).not.toBe(
        en["marvel0.cmd.field.chunk_accel_alpha_arm"],
      );
      const { host } = await openArm(arm("S1"), true);
      await click(accel(host));
      const said = text(box(host));
      expect(said).toContain("S1");
      expect(said).toContain("0.01");
      expect(said).toContain("set_shaping.py");
      expect(said).not.toMatch(/[{}]/);
    });

    test("the server's refusal of another alpha lands under the select, with the alpha it wants and the way out of a continued run", async () => {
      const { host } = await open(
        {
          preview: refused(422, "chunk_accel_alpha_arm_mismatch", {
            field: "chunk_accel_alpha",
            alpha: "0.01",
          }),
        },
        readDeploy(idle()),
        false,
        { rtc: false },
      );
      await click(accel(host));
      await make(host);
      expect(text(box(host))).toContain("anyspeedup");
      expect(text(box(host))).toContain("0.01");
      expect(text(box(host))).toContain("make a new command");
    });

    test("the server's refusal of a ramp rule lands on the rule choice", async () => {
      const { host } = await open(
        {
          preview: refused(422, "chunk_accel_rule_arm_preset", {
            field: "chunk_accel_rule",
          }),
        },
        readDeploy(idle()),
        false,
        { rtc: false },
      );
      await click(accel(host));
      await make(host);
      expect(text(box(host))).toContain(
        en["marvel0.err.chunk_accel_rule_arm_preset"],
      );
    });
  });

  test("the lambda and the alpha belong to a mode of the form too: under RTC and VLASH they are shown and sent", async () => {
    const { host, calls } = await open(undefined, readDeploy(idle()), false, {
      rtc: true,
      inferenceMode: "rtc",
    });
    await click(accel(host));
    expect(lambda(host)).not.toBeNull();
    await pick(lambda(host), "2.5");
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      inference_mode: "rtc",
      rtc: true,
      chunk_accel_rule: "v3",
      chunk_accel_lambda: 2.5,
      chunk_accel_alpha: 0.005,
    });
  });

  test("the server's refusal of a number lands under its select, in the page's words", async () => {
    const { host } = await open(
      {
        preview: refused(
          422,
          "chunk_accel_lambda_invalid",
          { field: "chunk_accel_lambda" },
          "English lambda sentence",
        ),
      },
      readDeploy(idle()),
      false,
      { rtc: false },
    );
    await click(accel(host));
    await pick(lambda(host), "2.5");
    await make(host);
    expect(text(box(host).querySelector(".ds-field__error"))).toBe(
      en["marvel0.err.chunk_accel_lambda_invalid"],
    );
    expect(text(card(host))).not.toContain("English lambda sentence");
  });

  test("the group placeholder follows the rule and the mode", async () => {
    const { host, choose } = await open(undefined, readDeploy(idle()), false, {
      rtc: false,
    });
    const group = () =>
      (field(card(host), "Rollout group") as HTMLInputElement).placeholder;
    await click(accel(host));
    expect(group()).toBe(`${SFT}_accel3_demo`);
    await click(rule(host, "v1")!);
    expect(group()).toBe(`${SFT}_accel_demo`);
    await click(rule(host, "v2")!);
    expect(group()).toBe(`${SFT}_accel_v2_demo`);
    await click(rule(host, "v3")!);
    await choose({ rtc: true });
    expect(group()).toBe(`${SFT}_rtc_accel3_demo`);
    await choose({ rtc: false, inferenceMode: "vlash" });
    expect(group()).toBe(`${SFT}_vlash_accel3_demo`);
  });

  test("turned off, the choice is kept but nothing is sent; on again it is still v2", async () => {
    const { host, calls } = await open(undefined, readDeploy(idle()), false, {
      rtc: false,
    });
    await click(accel(host));
    await click(rule(host, "v2")!);
    await click(accel(host));
    expect(rule(host, "v2")).toBeNull();
    await make(host);
    expect(calls.preview[0]).not.toHaveProperty("chunk_accel");
    expect(calls.preview[0]).not.toHaveProperty("chunk_accel_rule");
    await click(accel(host));
    expect(rule(host, "v2")!.checked).toBe(true);
  });

  test("with RTC chosen v1 and v2 are disabled and v3 is the rule sent; back to plain it is still v2", async () => {
    const { host, calls, choose } = await open(
      undefined,
      readDeploy(idle()),
      false,
      { rtc: false },
    );
    await click(accel(host));
    await click(rule(host, "v2")!);
    await choose({ rtc: true });
    expect(rule(host, "v1")!.disabled).toBe(true);
    expect(rule(host, "v2")!.disabled).toBe(true);
    expect(rule(host, "v3")!.checked).toBe(true);
    await make(host);
    expect(calls.preview[0]).toMatchObject({ chunk_accel_rule: "v3" });
    await choose({ rtc: false });
    expect(accel(host).checked).toBe(true);
    expect(rule(host, "v2")!.checked).toBe(true);
    expect(rule(host, "v2")!.disabled).toBe(false);
  });

  test("editing every parameter of a v2 run with the server's group and note, then choosing v1, files it under v1's default", async () => {
    // review 2026-11-01: the saved v2 group and note were restored as typed text and sent with the v1 command
    const defaults = {
      ...savedV2,
      rollout_group: `${SFT}_accel_v2_demo`,
      note: `marvel0 page: ${SFT} chunk accel experimental rule=v2 horizon=7`,
    };
    const { host, calls } = await open(
      { preview: ok(preview()) },
      readDeploy(ready()),
      false,
      { rtc: false },
      defaults,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the saved run form" },
    );
    await click(button(host, en["marvel0.episodeChange.editAll"])!);
    const group = field(card(host), "Rollout group") as HTMLInputElement;
    const note = field(card(host), "Note") as HTMLInputElement;
    expect(group.value).toBe("");
    expect(note.value).toBe("");
    expect(group.placeholder).toBe(`${SFT}_accel_v2_demo`);
    await click(rule(host, "v1")!);
    expect(group.placeholder).toBe(`${SFT}_accel_demo`);
    await make(host);
    const body = calls.preview[calls.preview.length - 1];
    expect(body).toMatchObject({ chunk_accel: true });
    expect(body).not.toHaveProperty("chunk_accel_rule");
    expect(body).not.toHaveProperty("rollout_group");
    expect(body).not.toHaveProperty("note");
  });

  test("when the saved run goes away, its form stays without the server's group and note", async () => {
    const defaults = {
      ...savedV2,
      rollout_group: `${SFT}_accel_v2_demo`,
      note: `marvel0 page: ${SFT} chunk accel experimental rule=v2 horizon=7`,
    };
    const { api, calls } = fakeApi({ preview: ok(preview()) });
    const status = readDeploy(ready());
    const panel = (resumeBody: Record<string, unknown> | null) => (
      <CommandPanel
        api={api}
        catalog={catalog()}
        status={status}
        selection={selectionFor(status, { rtc: false })}
        onUseAnother={() => {}}
        resumeBody={resumeBody}
      />
    );
    const view = await render(panel(defaults));
    await waitFor(
      () => view.host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the saved run form" },
    );
    await view.rerender(panel(null));
    const { host } = view;
    expect(host.querySelector("[data-testid=marvel0-command-resumed]")).toBe(
      null,
    );
    const group = field(card(host), "Rollout group") as HTMLInputElement;
    expect(group.value).toBe("");
    expect((field(card(host), "Note") as HTMLInputElement).value).toBe("");
    expect(rule(host, "v2")!.checked).toBe(true);
    await click(rule(host, "v1")!);
    expect(group.placeholder).toBe(`${SFT}_accel_demo`);
    await make(host);
    const body = calls.preview[calls.preview.length - 1];
    expect(body).not.toHaveProperty("rollout_group");
    expect(body).not.toHaveProperty("note");
  });

  test("editing every parameter keeps a group and note the person typed", async () => {
    const { host, calls } = await open(
      { preview: ok(preview()) },
      readDeploy(ready()),
      false,
      { rtc: false },
      savedV2,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the saved run form" },
    );
    await click(button(host, en["marvel0.episodeChange.editAll"])!);
    await click(rule(host, "v1")!);
    await make(host);
    expect(calls.preview[calls.preview.length - 1]).toMatchObject({
      rollout_group: "original_group",
      note: "original note",
    });
  });

  test("a resumed v2 run keeps v2 in the body, shown and disabled; editing every parameter keeps it", async () => {
    const { host, calls } = await open(
      { preview: ok(preview()) },
      readDeploy(ready()),
      false,
      { rtc: false },
      savedV2,
    );
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-command-resumed]"),
      { label: "the saved run form" },
    );
    expect(rule(host, "v2")!.checked).toBe(true);
    expect(rule(host, "v2")!.disabled).toBe(true);
    expect(rule(host, "v1")!.disabled).toBe(true);
    await type(field(host, "Task instruction")!, "new task");
    await make(host);
    expect(calls.preview[0]).toEqual({ ...savedV2, prompt: "new task" });
    await click(button(host, en["marvel0.episodeChange.editAll"])!);
    expect(rule(host, "v2")!.checked).toBe(true);
    expect(rule(host, "v2")!.disabled).toBe(false);
    await make(host);
    expect(calls.preview[1]).toMatchObject({
      chunk_accel: true,
      chunk_accel_rule: "v2",
    });
  });

  test("the server's refusal of the rule lands under the choice, in the page's words", async () => {
    const { host } = await open(
      {
        preview: refused(
          422,
          "chunk_accel_rule_invalid",
          { field: "chunk_accel_rule" },
          "English rule sentence",
        ),
      },
      readDeploy(idle()),
      false,
      { rtc: false },
    );
    await click(accel(host));
    await click(rule(host, "v2")!);
    await make(host);
    expect(text(box(host).querySelector(".ds-field__error"))).toBe(
      en["marvel0.err.chunk_accel_rule_invalid"],
    );
    expect(rule(host, "v2")!.getAttribute("aria-invalid")).toBe("true");
    const described = rule(host, "v2")!.getAttribute("aria-describedby")!;
    expect(text(card(host).querySelector(`[id="${described}"]`))).toBe(
      en["marvel0.err.chunk_accel_rule_invalid"],
    );
    expect(text(card(host))).not.toContain("English rule sentence");
    expect(
      host.querySelector("[data-testid=marvel0-command-problem]"),
    ).toBeNull();
  });

  test("the result names the rule the server answered", async () => {
    const { host } = await open(
      {
        preview: ok(
          preview({
            control: {
              hz: 10,
              chunk_accel: {
                enabled: true,
                experimental: true,
                speed_m_s: 0.25,
                tau_s: 0.2,
                rule: "v2",
              },
            },
          }),
        ),
      },
      readDeploy(idle()),
      false,
      { rtc: false },
    );
    await click(accel(host));
    await click(rule(host, "v2")!);
    await make(host);
    const line = host.querySelector(
      "[data-testid=marvel0-command-chunk-accel-on]",
    );
    expect(text(line)).toContain("rule v2");
    expect(text(line)).toContain("up to 0.25 m/s");
  });
});

describe("the model, RTC and guidance strength follow the policy server card", () => {
  const follow = (host: HTMLElement) =>
    host.querySelector("[data-testid=marvel0-command-follow]") as HTMLElement;

  test("a read-only badge says which model the command follows, and the way back to choose another", async () => {
    const { host, used } = await open();
    expect(text(follow(host))).toContain(`follows the policy server: ${SFT}`);
    expect(follow(host).getAttribute("data-source")).toBe("default");
    expect(text(follow(host))).toContain("Mode: RTC");
    await click(button(follow(host), "use another model")!);
    expect(used).toEqual(["another"]);
  });

  test("while a server of this page runs a model, that model is the command's, with its RTC and strength", async () => {
    const { host, calls } = await open(
      { preview: ok(preview()) },
      readDeploy(
        ready({
          deploy: deployInfo({
            model_id: CFG,
            rtc: false,
            cfg_guidance_scale: 1.5,
          }),
        }),
      ),
    );
    expect(follow(host).getAttribute("data-source")).toBe("server");
    expect(text(follow(host))).toContain(`follows the policy server: ${CFG}`);
    expect(text(follow(host))).toContain("Mode: Plain inference");
    expect(text(follow(host))).toContain("Guidance strength 1.5");
    await make(host);
    expect(calls.preview[0]).toMatchObject({ model_id: CFG, rtc: false });
    // No mismatch to warn about: the command asks for what the server serves.
    expect(
      host
        .querySelector("[data-testid=marvel0-command-server]")!
        .getAttribute("data-kind"),
    ).toBe("match");
  });

  test("a model chosen in the policy server card is followed at once", async () => {
    const { host, calls, choose } = await open();
    await choose({ modelId: CFG, rtc: false, source: "choice" });
    expect(follow(host).getAttribute("data-source")).toBe("choice");
    expect(text(follow(host))).toContain(`follows the policy server: ${CFG}`);
    await make(host);
    expect(calls.preview[0]).toMatchObject({ model_id: CFG, rtc: false });
  });

  test("the default rollout group carries the full checkpoint name", async () => {
    const { host, choose } = await open();
    const group = () =>
      (field(card(host), "Rollout group") as HTMLInputElement).placeholder;
    expect(group()).toBe(`${SFT}_rtc_demo`);
    await choose({ modelId: CFG });
    expect(group()).toBe(`${CFG}_rtc_demo`);
    await choose({ modelId: CFG, rtc: false });
    expect(group()).toBe(`${CFG}_demo`);
  });
});

describe("making the command", () => {
  test("VLASH selection follows into preview with its own scheduler and overlap", async () => {
    const { host, calls } = await open(undefined, readDeploy(idle()), false, {
      rtc: false,
      inferenceMode: "vlash",
      vlashOverlapSteps: "3",
    });
    expect(
      text(host.querySelector("[data-testid=marvel0-command-follow]")),
    ).toContain("Mode: VLASH");
    expect(
      (field(card(host), "Rollout group") as HTMLInputElement).placeholder,
    ).toBe(`${SFT}_vlash_demo`);
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      inference_mode: "vlash",
      rtc: false,
      vlash_overlap_steps: 3,
    });
  });

  test("sends the form once and shows the command to copy, with what server it needs", async () => {
    withClipboard();
    const { host, calls } = await open();
    await make(host);
    expect(calls.preview).toEqual([
      {
        model_id: SFT,
        prompt: "pick the eggplant in the blue plate",
        max_steps: 500,
        eval_num: 5,
        min_z: 0.025,
        rtc: true,
        inference_mode: "rtc",
      },
    ]);
    const result = host.querySelector("[data-testid=marvel0-command-result]")!;
    const pre = result.querySelector("[data-testid=marvel0-command-text]")!;
    expect(pre.tagName).toBe("PRE");
    expect(pre.getAttribute("lang")).toBe("en");
    expect(pre.textContent).toBe(COMMAND);
    expect(text(result)).toContain(
      `This command needs a policy server deployed for ${SFT}, mode RTC.`,
    );
    expect(text(result)).toContain(en["marvel0.cmd.hands"]);
    expect(text(result)).toContain("This page has not run it.");
    await click(button(result as HTMLElement, "Copy")!);
    await tick();
    expect(clipboard).toEqual([COMMAND]);
    expect(button(result as HTMLElement, "Copied")).toBeDefined();
  });

  test("copying the command for a terminal first lets the page stop the camera preview, and says so", async () => {
    withClipboard();
    const order: string[] = [];
    const { host } = await open(undefined, undefined, false, {}, null, () => {
      order.push(`stop (clipboard has ${clipboard.length})`);
      return true;
    });
    await make(host);
    const result = host.querySelector(
      "[data-testid=marvel0-command-result]",
    ) as HTMLElement;
    expect(
      result.querySelector("[data-testid=marvel0-command-preview-stopped]"),
    ).toBeNull();
    await click(button(result, "Copy")!);
    await tick();
    expect(order).toEqual(["stop (clipboard has 0)"]); // before the copy
    expect(clipboard).toEqual([COMMAND]);
    expect(
      result.querySelector("[data-testid=marvel0-command-preview-stopped]")
        ?.textContent,
    ).toBe(en["marvel0.cmd.result.preview_stopped"]);
  });

  test("a copy that stopped no preview says nothing about one", async () => {
    withClipboard();
    const { host } = await open(
      undefined,
      undefined,
      false,
      {},
      null,
      () => false,
    );
    await make(host);
    const result = host.querySelector(
      "[data-testid=marvel0-command-result]",
    ) as HTMLElement;
    await click(button(result, "Copy")!);
    await tick();
    expect(clipboard).toEqual([COMMAND]);
    expect(
      result.querySelector("[data-testid=marvel0-command-preview-stopped]"),
    ).toBeNull();
  });

  test("without a clipboard the command is selected so that Ctrl+C copies it", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    const { host } = await open();
    await make(host);
    const result = host.querySelector(
      "[data-testid=marvel0-command-result]",
    ) as HTMLElement;
    await click(button(result, "Copy")!);
    await tick();
    const selection = window.getSelection()!;
    const pre = result.querySelector("pre")!;
    expect(selection.anchorNode).toBe(pre);
    expect(selection.toString()).toContain("--levi-mode=off");
  });

  test("nothing but the preview is asked: the card cannot deploy, stop or start anything", async () => {
    const { host, calls } = await open();
    await make(host);
    expect(calls.preview).toHaveLength(1);
    expect(calls.startDeploy).toHaveLength(0);
    expect(calls.stopDeploy).toHaveLength(0);
  });

  test("what is typed is what is asked for: horizon, group, note, and the selected model with RTC off", async () => {
    const { host, calls } = await open(undefined, undefined, false, {
      modelId: CFG,
      rtc: false,
    });
    const c = card(host);
    await type(field(c, "Open-loop horizon"), "4");
    await type(field(c, "Rollout group"), "my_group");
    await type(field(c, "Note"), "first try");
    await type(
      field(c, "Task instruction"),
      "stack the plates of same color together",
    );
    await make(host);
    expect(calls.preview[0]).toMatchObject({
      model_id: CFG,
      rtc: false,
      rtc_horizon: 4,
      rollout_group: "my_group",
      note: "first try",
      prompt: "stack the plates of same color together",
    });
  });

  test("RTC changes the horizon's name and the group the server would use", async () => {
    const { host, choose } = await open();
    const c = card(host);
    expect(field(c, "RTC execute horizon")).not.toBeNull();
    await choose({ rtc: false });
    expect(field(c, "Open-loop horizon")).not.toBeNull();
    expect(field(c, "RTC execute horizon")).toBeNull();
    expect((field(c, "Rollout group") as HTMLInputElement).placeholder).toBe(
      `${SFT}_demo`,
    );
    expect(text(c)).toContain("A whole number from 1 to 10. Empty uses 6.");
  });

  test("a number that is not a number stops at the field, with no request", async () => {
    const { host, calls } = await open();
    const c = card(host);
    await type(field(c, "Max steps per episode"), "5.5");
    await type(field(c, "Lowest height"), "abc");
    await make(host);
    expect(calls.preview).toHaveLength(0);
    expect(text(c)).toContain("Enter a whole number.");
    expect(text(c)).toContain("Enter a number.");
  });

  test("the server's refusal of a field shows on that field in the page's words", async () => {
    const { host } = await open({
      preview: refused(
        422,
        "min_z_below_floor",
        { field: "min_z", floor: 0.025 },
        "English min_z sentence",
      ),
    });
    const c = card(host);
    await type(field(c, "Lowest height"), "0.01");
    await make(host);
    const errorFor = (label: string) => {
      const input = field(c, label)!;
      return input.closest(".ds-field")?.querySelector(".ds-field__error")
        ?.textContent;
    };
    expect(errorFor("Lowest height")).toBe(
      "min-z cannot go below the site floor, 0.025 m.",
    );
    expect(errorFor("Task instruction")).toBeUndefined();
    expect(text(c)).not.toContain("English min_z sentence");
    expect(
      host.querySelector("[data-testid=marvel0-command-result]"),
    ).toBeNull();
  });

  test("each rule about a task instruction is worded", async () => {
    for (const [reason, word] of [
      ["empty", "Enter a task instruction."],
      ["too_long", "at most 300 characters"],
      ["forbidden_char", "quote"],
      ["leading_dash", "dash"],
      ["control_char", "control characters"],
    ] as const) {
      const { host, calls } = await open({
        preview: refused(422, "prompt_invalid", { field: "prompt", reason }),
      });
      await make(host);
      expect(calls.preview).toHaveLength(1);
      const error = field(card(host), "Task instruction")!
        .closest(".ds-field")!
        .querySelector(".ds-field__error")!;
      expect(error.textContent).toContain(word);
    }
  });

  test("a model that takes only the known instructions says so when the server refuses one", async () => {
    const { host } = await open({
      preview: refused(422, "prompt_unknown_for_model", { field: "prompt" }),
    });
    await make(host);
    expect(text(card(host))).toContain(
      "only takes the known task instructions",
    );
  });

  test("FastAPI's refusal of a body lands on the field it names", async () => {
    const { host } = await open({
      preview: {
        ok: false,
        status: 422,
        code: "validation",
        message: "Input should be a valid number",
        extra: {},
        field: "min_z",
        fields: [{ field: "min_z", message: "Input should be a valid number" }],
      },
    });
    await make(host);
    const error = field(card(host), "Lowest height")!
      .closest(".ds-field")!
      .querySelector(".ds-field__error");
    expect(error?.textContent).toBe(
      "The value has the wrong type for this field.",
    );
  });

  test("a refusal that is about no field is shown once, above the result", async () => {
    const { host } = await open({
      preview: refused(500, "command_self_check", {}, "internal"),
    });
    await make(host);
    const problem = host.querySelector(
      "[data-testid=marvel0-command-problem]",
    )!;
    expect(text(problem)).toContain("failed its own check");
    expect(text(problem)).not.toContain("internal");
    expect(
      host.querySelector("[data-testid=marvel0-command-result]"),
    ).toBeNull();
  });

  test("no answer at all is said, and the form stays", async () => {
    const { host } = await open({ preview: nothing("fetch failed") });
    await make(host);
    const problem = host.querySelector(
      "[data-testid=marvel0-command-problem]",
    )!;
    expect(text(problem)).toContain("did not answer");
    expect(text(problem)).toContain("fetch failed");
  });

  test("an answer that is not a command is not shown as one", async () => {
    const { host } = await open({
      preview: ok({ schema: "levi.marvel0.command.v1", ok: true }),
    });
    await make(host);
    expect(host.querySelector("[data-testid=marvel0-command-text]")).toBeNull();
    expect(
      host.querySelector("[data-testid=marvel0-command-problem]"),
    ).not.toBeNull();
  });

  test("warnings are listed in words and never stop the command", async () => {
    const { host } = await open({
      preview: ok(
        preview({
          warnings: [
            { code: "unverified_model", text: "English 1" },
            { code: "min_z_below_task_table", text: "English 2" },
            { code: "something_new", text: "English 3" },
          ],
        }),
      ),
    });
    await make(host);
    const warnings = host.querySelector(
      "[data-testid=marvel0-command-warnings]",
    )!;
    expect(text(warnings)).toContain(
      "No real-GPU serving check is recorded for this model",
    );
    expect(text(warnings)).toContain("fingertips can reach the table");
    expect(warnings.querySelector("[lang=en]")?.textContent).toBe("English 3");
    expect(
      host.querySelector("[data-testid=marvel0-command-text]"),
    ).not.toBeNull();
  });

  test("a form that changed after the command was made cannot be copied as if it still matched", async () => {
    withClipboard();
    const { host } = await open();
    await make(host);
    const result = () =>
      host.querySelector("[data-testid=marvel0-command-result]") as HTMLElement;
    expect(button(result(), "Copy")).toBeDefined();
    await type(field(card(host), "Eval number"), "6");
    expect(result().getAttribute("data-stale")).toBe("true");
    expect(button(result(), "Copy")).toBeUndefined();
    expect(text(result())).toContain(en["marvel0.cmd.result.stale"]);
    // Back to the same form: it matches again.
    await type(field(card(host), "Eval number"), "5");
    expect(result().getAttribute("data-stale")).toBeNull();
    expect(button(result(), "Copy")).toBeDefined();
  });

  test("a press while the request is open does not send a second one", async () => {
    let release: (value: ReturnType<typeof ok>) => void = () => {};
    const pending = new Promise<ReturnType<typeof ok>>((resolve) => {
      release = resolve;
    });
    const { host, calls } = await open({ preview: () => pending });
    await click(button(card(host), "Make the command")!);
    await click(button(card(host), "Make the command")!);
    expect(calls.preview).toHaveLength(1);
    await act(async () => release(ok(preview())));
    await tick();
    expect(calls.preview).toHaveLength(1);
  });

  test("two presses in the same instant send one request", async () => {
    const { host, calls } = await open();
    const make = button(card(host), "Make the command")!;
    await act(async () => {
      make.click();
      make.click();
    });
    await tick();
    expect(calls.preview).toHaveLength(1);
  });

  test("submitting the form with Enter makes the command", async () => {
    const { host, calls } = await open();
    const form = card(host).querySelector("form")!;
    await fire(form, new Event("submit", { bubbles: true, cancelable: true }));
    await tick();
    expect(calls.preview).toHaveLength(1);
  });
});

describe("the policy server against the command", () => {
  const line = (host: HTMLElement) =>
    host.querySelector("[data-testid=marvel0-command-server]") as HTMLElement;

  test("a ready server of the same model fits", async () => {
    const { host } = await open(undefined, readDeploy(ready()));
    expect(line(host).getAttribute("data-kind")).toBe("match");
    expect(text(line(host))).toContain("deployed for this model and mode");
  });

  test("no server, a server still starting, another model, a server without RTC", async () => {
    const none = await open(undefined, readDeploy(idle()));
    expect(line(none.host).getAttribute("data-kind")).toBe("no_server");
    const starting_ = await open(undefined, readDeploy(starting()));
    expect(line(starting_.host).getAttribute("data-kind")).toBe("starting");
    // The page's selection follows the server, so these two cannot arise from
    // the page itself; the card still words them if it is handed a selection
    // that differs from what the server serves.
    const other = await open(
      undefined,
      readDeploy(ready({ deploy: deployInfo({ model_id: CFG }) })),
      false,
      { modelId: SFT },
    );
    expect(text(line(other.host))).toContain(CFG);
    const plain = await open(
      undefined,
      readDeploy(ready({ deploy: deployInfo({ rtc: false }) })),
      false,
      { rtc: true },
    );
    expect(line(plain.host).getAttribute("data-kind")).toBe("needs_rtc");
  });

  test("an unread state is said not to be known", async () => {
    const { host } = await open(undefined, null);
    expect(line(host).getAttribute("data-kind")).toBe("unknown");
  });
});

describe("in Chinese", () => {
  test("the card, a warning and a field refusal are worded without an English sentence", async () => {
    const { host } = await open(
      {
        preview: ok(
          preview({
            warnings: [{ code: "rtc_margin_low", text: "ENGLISH WARNING" }],
          }),
        ),
      },
      readDeploy(ready()),
      true,
    );
    await waitFor(() => text(card(host)).includes(zh["marvel0.cmd.title"]), {
      label: "Chinese",
    });
    const c = card(host);
    expect(text(c)).toContain(zh["marvel0.cmd.scope"]);
    await click(button(c, zh["marvel0.cmd.btn.make"])!);
    await tick();
    const body = text(card(host));
    expect(body).toContain(zh["marvel0.cmd.warning.rtc_margin_low"]);
    expect(body).not.toContain("ENGLISH WARNING");
    expect(body).toContain(zh["marvel0.cmd.hands"]);
    expect(body).toContain(zh["marvel0.cmd.server.match"]);
  });

  test("a refused field shows the Chinese sentence with the limit", async () => {
    const { host } = await open(
      {
        preview: refused(
          422,
          "max_steps_out_of_range",
          { field: "max_steps", min: 1, max: 3000 },
          "ENGLISH RANGE",
        ),
      },
      readDeploy(idle()),
      true,
    );
    await waitFor(() => text(card(host)).includes(zh["marvel0.cmd.title"]), {
      label: "Chinese",
    });
    await click(button(card(host), zh["marvel0.cmd.btn.make"])!);
    await tick();
    const error = field(
      card(host),
      zh["marvel0.cmd.field.max_steps"].slice(0, 6),
    )!
      .closest(".ds-field")!
      .querySelector(".ds-field__error")!;
    expect(error.textContent).toBe("请输入 1 到 3000 之间的整数。");
  });
});

void model;
void cfgModel;
