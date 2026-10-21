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
import type { Marvel0Catalog } from "../use-marvel0-actions";
import { modelsAnswer, siteAnswer, model, cfgModel } from "./catalog-fixtures";
import { COMMAND, preview } from "./command-fixtures";
import { deployInfo, idle, ready, starting } from "./deploy-fixtures";
import {
  button,
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
  return { ...effectiveSelection(models, NO_CHOICE, status)!, ...over };
}

async function open(
  canned: Canned = { preview: ok(preview()) },
  status: ReturnType<typeof readDeploy> = readDeploy(idle()),
  zhLanguage = false,
  over: Partial<Effective> = {},
) {
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const { api, calls } = fakeApi(canned);
  const used: string[] = [];
  const panel = (selection: Effective) => {
    const card = (
      <CommandPanel
        api={api}
        catalog={catalog()}
        status={status}
        selection={selection}
        onUseAnother={() => used.push("another")}
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
    expect(inputs).toBe(7); // prompt, steps, eval, min z, horizon, group, note
    expect(card(host).querySelectorAll("select").length).toBe(0);
    expect(card(host).querySelector("[role=switch]")).toBeNull();
  });
});

describe("the model, RTC and guidance strength follow the policy server card", () => {
  const follow = (host: HTMLElement) =>
    host.querySelector("[data-testid=marvel0-command-follow]") as HTMLElement;

  test("a read-only badge says which model the command follows, and the way back to choose another", async () => {
    const { host, used } = await open();
    expect(text(follow(host))).toContain(`follows the policy server: ${SFT}`);
    expect(follow(host).getAttribute("data-source")).toBe("default");
    expect(text(follow(host))).toContain("RTC on");
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
    expect(text(follow(host))).toContain("RTC off");
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
      },
    ]);
    const result = host.querySelector("[data-testid=marvel0-command-result]")!;
    const pre = result.querySelector("[data-testid=marvel0-command-text]")!;
    expect(pre.tagName).toBe("PRE");
    expect(pre.getAttribute("lang")).toBe("en");
    expect(pre.textContent).toBe(COMMAND);
    expect(text(result)).toContain(
      `This command needs a policy server deployed for ${SFT}, RTC on.`,
    );
    expect(text(result)).toContain(en["marvel0.cmd.hands"]);
    expect(text(result)).toContain("This page has not run it.");
    await click(button(result as HTMLElement, "Copy")!);
    await tick();
    expect(clipboard).toEqual([COMMAND]);
    expect(button(result as HTMLElement, "Copied")).toBeDefined();
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
