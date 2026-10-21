import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { readModels, readSite } from "../catalog-logic";
import {
  bodyKey,
  commandBody,
  initialCommandForm,
  placeholders,
  readPreview,
  serverMatch,
  warningSay,
  type CommandForm,
} from "../command-logic";
import { readDeploy } from "../deploy-logic";
import { sayText } from "../marvel0-say";
import { modelsAnswer, siteAnswer } from "./catalog-fixtures";
import { COMMAND, preview } from "./command-fixtures";
import { deployInfo, idle, ready, starting } from "./deploy-fixtures";

const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? en : zh)[key as keyof typeof en] ?? key;
const models = readModels(modelsAnswer())!;
const site = readSite(siteAnswer())!;
const base = (over: Partial<CommandForm> = {}): CommandForm => ({
  ...initialCommandForm(models, site),
  ...over,
});

describe("the form as it opens", () => {
  test("holds the site's defaults and leaves what the server fills in empty", () => {
    expect(initialCommandForm(models, site)).toEqual({
      modelId: "pi05_fr3_all_step49999",
      prompt: "pick the eggplant in the blue plate",
      maxSteps: "500",
      evalNum: "5",
      minZ: "0.025",
      rtc: true,
      horizon: "",
      group: "",
      note: "",
      hz: "10",
    });
  });

  test("starts on the model asked for when the list has it", () => {
    expect(
      initialCommandForm(models, site, "recap_cfg_r2_best_step14300_jax")
        .modelId,
    ).toBe("recap_cfg_r2_best_step14300_jax");
    expect(initialCommandForm(models, site, "gone").modelId).toBe(
      "pi05_fr3_all_step49999",
    );
  });

  test("RTC follows the server's default", () => {
    const off = readModels(
      modelsAnswer({
        defaults: { rtc: false, fraction: 0.22, cfg_guidance_scale: 1 },
      }),
    )!;
    expect(initialCommandForm(off, site).rtc).toBe(false);
  });
});

describe("the request body", () => {
  test("a default form asks for the model, the instruction and the numbers", () => {
    const { body, errors } = commandBody(base());
    expect(errors).toEqual({});
    expect(body).toEqual({
      model_id: "pi05_fr3_all_step49999",
      prompt: "pick the eggplant in the blue plate",
      max_steps: 500,
      eval_num: 5,
      min_z: 0.025,
      rtc: true,
    });
  });

  test("what is filled in is sent, what is empty is left to the server", () => {
    const { body } = commandBody(
      base({
        horizon: "7",
        group: "my_group",
        note: "first try",
        maxSteps: "",
      }),
    );
    expect(body).toEqual({
      model_id: "pi05_fr3_all_step49999",
      prompt: "pick the eggplant in the blue plate",
      eval_num: 5,
      min_z: 0.025,
      rtc: true,
      rtc_horizon: 7,
      rollout_group: "my_group",
      note: "first try",
    });
  });

  test("RTC off is sent as false, with the open-loop horizon in the same field", () => {
    expect(commandBody(base({ rtc: false, horizon: "4" })).body).toMatchObject({
      rtc: false,
      rtc_horizon: 4,
    });
  });

  test("text is sent as typed, so the server's rules are the ones that apply", () => {
    expect(
      commandBody(base({ prompt: "  pick it ", note: " n " })).body,
    ).toMatchObject({
      prompt: "  pick it ",
      note: " n ",
    });
  });

  test("a number that is not a number is an error on its field, in the page's words", () => {
    const { errors } = commandBody(
      base({ maxSteps: "5.5", evalNum: "x", minZ: "abc", horizon: "1.5" }),
    );
    expect(Object.keys(errors).sort()).toEqual([
      "eval_num",
      "max_steps",
      "min_z",
      "rtc_horizon",
    ]);
    expect(sayText(errors.max_steps, catalogue("en"))).toBe(
      "Enter a whole number.",
    );
    expect(sayText(errors.min_z, catalogue("en"))).toBe("Enter a number.");
    expect(sayText(errors.min_z, catalogue("zh"))).toMatch(/[一-鿿]/);
  });

  test("only plain decimals are numbers: no hex, no spaces inside, no infinity", () => {
    for (const bad of [
      "0x10",
      "1 2",
      "Infinity",
      "NaN",
      "1e999",
      "--1",
      ".",
      "+",
    ])
      expect(Object.keys(commandBody(base({ minZ: bad })).errors), bad).toEqual(
        ["min_z"],
      );
    expect(commandBody(base({ minZ: " 0.03 " })).body.min_z).toBe(0.03);
    expect(commandBody(base({ minZ: "3e-2" })).body.min_z).toBe(0.03);
  });

  test("a negative number is sent, so the server can say why it is refused", () => {
    expect(commandBody(base({ minZ: "-1" })).body.min_z).toBe(-1);
  });

  test("a whole number needs digits only", () => {
    for (const bad of ["1e2", "1.0", "0x1", "- 1"])
      expect(
        Object.keys(commandBody(base({ evalNum: bad })).errors),
        bad,
      ).toEqual(["eval_num"]);
    expect(commandBody(base({ evalNum: "07" })).body.eval_num).toBe(7);
  });

  test("the same form gives the same key, a changed one a different key", () => {
    const a = bodyKey(commandBody(base()).body);
    expect(bodyKey(commandBody(base()).body)).toBe(a);
    expect(bodyKey(commandBody(base({ prompt: "other" })).body)).not.toBe(a);
  });
});

describe("what the form shows as its defaults", () => {
  test("the group of the model for the mode, the horizon of the site for the mode", () => {
    expect(placeholders(base(), models, site)).toEqual({
      group: "pi05_fr3_all_step49999_rtc_demo",
      horizon: "6",
      horizonMax: 8,
      horizonLoweredFrom: null,
    });
    expect(placeholders(base({ rtc: false }), models, site)).toEqual({
      group: "pi05_fr3_all_step49999_demo",
      horizon: "6",
      horizonMax: 10,
      horizonLoweredFrom: null,
    });
  });

  test("a model that is not in the list has no group to show", () => {
    expect(placeholders(base({ modelId: "gone" }), models, site).group).toBe(
      "",
    );
  });
});

describe("readPreview", () => {
  test("reads the command and what server it needs", () => {
    expect(readPreview(preview())).toEqual({
      command: COMMAND,
      needs: { modelId: "pi05_fr3_all_step49999", rtc: true },
      verified: true,
      warnings: [],
    });
  });

  test("keeps the warnings with their codes", () => {
    const view = readPreview(
      preview({
        warnings: [
          { code: "unverified_model", text: "English" },
          { code: "x", text: "Other" },
        ],
      }),
    )!;
    expect(view.warnings).toEqual([
      { code: "unverified_model", text: "English" },
      { code: "x", text: "Other" },
    ]);
  });

  test("an answer without a command, or not ok, or of another schema is none", () => {
    expect(readPreview(preview({ command: "" }))).toBeNull();
    expect(readPreview(preview({ command: 3 }))).toBeNull();
    expect(readPreview(preview({ ok: false }))).toBeNull();
    expect(
      readPreview(preview({ schema: "levi.marvel0.command.v2" })),
    ).toBeNull();
    expect(readPreview(preview({ server_requirements: null }))).toBeNull();
    expect(
      readPreview(preview({ server_requirements: { model_id: "m" } })),
    ).toBeNull();
    expect(readPreview(null)).toBeNull();
  });
});

describe("warnings", () => {
  test("the three the server gives are worded in both languages", () => {
    for (const code of [
      "unverified_model",
      "rtc_margin_low",
      "min_z_below_task_table",
    ]) {
      const worded = warningSay({ code, text: "English" });
      expect(worded.say, code).not.toBeNull();
      expect(sayText(worded.say!, catalogue("zh"))).toMatch(/[一-鿿]/);
      expect(sayText(worded.say!, catalogue("en"))).not.toMatch(/^marvel0\./);
    }
  });

  test("a warning the page does not know is shown as written", () => {
    expect(warningSay({ code: "new", text: "Be careful" })).toEqual({
      say: null,
      text: "Be careful",
    });
  });
});

describe("whether the policy server fits the command", () => {
  const match = (
    data: Record<string, unknown>,
    modelId: string,
    rtc: boolean,
  ) => serverMatch({ modelId, rtc }, readDeploy(data));
  const sft = "pi05_fr3_all_step49999";

  test("a ready server of the same model and mode fits", () => {
    expect(match(ready(), sft, true).kind).toBe("match");
    expect(match(ready(), sft, false).kind).toBe("match");
  });

  test("a client with RTC needs a server with RTC", () => {
    const plain = ready({ deploy: deployInfo({ rtc: false }) });
    expect(match(plain, sft, true).kind).toBe("needs_rtc");
    expect(match(plain, sft, false).kind).toBe("match");
  });

  test("another model does not fit, and says which one runs", () => {
    const out = match(ready(), "recap_cfg_r2_best_step14300_jax", true);
    expect(out.kind).toBe("other_model");
    expect(sayText(out.say, catalogue("en"))).toContain(sft);
  });

  test("a server that is still starting is not ready to run the command against", () => {
    expect(match(starting(), sft, true).kind).toBe("starting");
  });

  test("no server of this page, a failed one, or an exited one is no server", () => {
    for (const data of [
      idle(),
      idle({ state: "failed" }),
      idle({ state: "dead" }),
    ])
      expect(match(data, sft, true).kind).toBe("no_server");
  });

  test("a server started by hand cannot be checked", () => {
    expect(match(idle({ state: "foreign_listener" }), sft, true).kind).toBe(
      "by_hand",
    );
  });

  test("a state that is not known is said not to be known", () => {
    expect(match(idle({ state: "unknown" }), sft, true).kind).toBe("unknown");
    expect(serverMatch({ modelId: sft, rtc: true }, null).kind).toBe("unknown");
  });

  test("only a match is good news", () => {
    expect(match(ready(), sft, true).good).toBe(true);
    expect(match(idle(), sft, true).good).toBe(false);
    expect(match(starting(), sft, true).good).toBe(false);
  });

  test("every kind has words in both languages", () => {
    for (const data of [
      ready(),
      starting(),
      idle(),
      idle({ state: "foreign_listener" }),
      idle({ state: "unknown" }),
    ])
      for (const lang of ["en", "zh"] as const)
        expect(
          sayText(match(data, "x", true).say, catalogue(lang)),
        ).not.toMatch(/^marvel0\./);
  });
});
