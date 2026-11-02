import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { readModels, readSite } from "../catalog-logic";
import {
  CHUNK_ACCEL_RULES,
  CHUNK_ACCEL_V3_ALPHAS,
  CHUNK_ACCEL_V3_LAMBDAS,
  CHUNK_ACCEL_V3_MODES,
  bodyKey,
  chunkAccelAllowed,
  chunkAccelOn,
  chunkAccelRateOk,
  chunkAccelRuleDefault,
  chunkAccelRuleOf,
  chunkAccelRulesFor,
  commandFormFromRun,
  commandBody,
  editableSavedForm,
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
import deploySamples from "./marvel0-deploy.samples.json";

const catalogue = (lang: "en" | "zh") => (key: string) =>
  (lang === "en" ? en : zh)[key as keyof typeof en] ?? key;
const models = readModels(modelsAnswer())!;
const site = readSite(siteAnswer())!;
const base = (over: Partial<CommandForm> = {}): CommandForm => ({
  ...initialCommandForm(models, site),
  ...over,
});

describe("the form as it opens", () => {
  test("accepts only a complete saved run form and does not infer missing values", () => {
    const form = {
      model_id: "pi05_fr3_all_step49999",
      prompt: "pick the eggplant in the blue plate",
      max_steps: 500,
      eval_num: 5,
      min_z: 0.025,
      rtc: true,
      inference_mode: "rtc",
      rtc_horizon: 7,
      rollout_group: "saved_group",
      note: "saved note",
      control_hz: 20,
    };
    expect(commandFormFromRun(form)).toEqual({
      modelId: form.model_id,
      prompt: form.prompt,
      maxSteps: "500",
      evalNum: "5",
      minZ: "0.025",
      rtc: true,
      inferenceMode: "rtc",
      vlashOverlapSteps: "2",
      horizon: "7",
      group: "saved_group",
      note: "saved note",
      hz: "20",
    });
    const incomplete: Record<string, unknown> = { ...form };
    delete incomplete.max_steps;
    expect(commandFormFromRun(incomplete)).toBeNull();
  });

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
      inference_mode: "rtc",
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
      inference_mode: "rtc",
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

describe("in-chunk acceleration", () => {
  const plain = { rtc: false, inferenceMode: "plain" as const };

  test("is sent only when on, and with every inference mode (rule v3 runs with all three)", () => {
    expect(commandBody(base(plain)).body).not.toHaveProperty("chunk_accel");
    expect(
      commandBody(base({ ...plain, chunkAccel: false })).body,
    ).not.toHaveProperty("chunk_accel");
    expect(
      commandBody(base({ ...plain, chunkAccel: true })).body,
    ).toMatchObject({ inference_mode: "plain", chunk_accel: true });
    for (const mode of [
      { rtc: true, inferenceMode: "rtc" as const },
      { rtc: false, inferenceMode: "vlash" as const },
      // An old form without the mode: RTC on means RTC.
      { rtc: true },
    ]) {
      const body = commandBody(base({ ...mode, chunkAccel: true })).body;
      expect(body, JSON.stringify(mode)).toMatchObject({ chunk_accel: true });
      // rules v1 and v2 run with plain inference only: with these modes the rule can only be v3
      expect(body, JSON.stringify(mode)).toMatchObject({
        chunk_accel_rule: "v3",
      });
    }
    // and with a rule v1 or v2 remembered from plain, never v1 or v2 under another mode
    for (const rule of ["v1", "v2"] as const)
      expect(
        commandBody(
          base({
            rtc: true,
            inferenceMode: "rtc",
            chunkAccel: true,
            chunkAccelRule: rule,
          }),
        ).body,
      ).toMatchObject({ chunk_accel_rule: "v3" });
  });

  test("runs at 10 Hz only: at another rate it is not sent", () => {
    // final review 2026-10-31: the client refuses any other rate at launch
    expect(chunkAccelRateOk(base({ hz: "10" }))).toBe(true);
    expect(chunkAccelRateOk(base({ hz: "20" }))).toBe(false);
    expect(
      commandBody(base({ ...plain, chunkAccel: true, hz: "20" })).body,
    ).not.toHaveProperty("chunk_accel");
  });

  test("is allowed with plain, RTC and VLASH inference, the three modes rule v3 runs with", () => {
    expect([...CHUNK_ACCEL_V3_MODES]).toEqual(["plain", "rtc", "vlash"]);
    expect(chunkAccelAllowed(plain)).toBe(true);
    expect(chunkAccelAllowed({ rtc: false })).toBe(true);
    expect(chunkAccelAllowed({ rtc: true })).toBe(true);
    expect(chunkAccelAllowed({ rtc: true, inferenceMode: "rtc" })).toBe(true);
    expect(chunkAccelAllowed({ rtc: false, inferenceMode: "vlash" })).toBe(
      true,
    );
  });

  test("the switch is on for a form when it is chosen, the mode allows it and the rate is 10 Hz", () => {
    expect(chunkAccelOn(base({ ...plain, chunkAccel: true }))).toBe(true);
    expect(chunkAccelOn(base({ ...plain, chunkAccel: false }))).toBe(false);
    expect(chunkAccelOn(base(plain))).toBe(false);
    expect(chunkAccelOn(base({ ...plain, chunkAccel: true, hz: "20" }))).toBe(
      false,
    );
  });

  test("a changed switch changes the request key", () => {
    const off = bodyKey(commandBody(base(plain)).body);
    const on = bodyKey(commandBody(base({ ...plain, chunkAccel: true })).body);
    expect(on).not.toBe(off);
  });

  test("a saved run with it on round-trips into the form and back into the same body", () => {
    const saved = {
      model_id: "pi05_fr3_all_step49999",
      prompt: "pick the eggplant in the blue plate",
      max_steps: 500,
      eval_num: 5,
      min_z: 0.025,
      rtc: false,
      inference_mode: "plain",
      rtc_horizon: 7,
      rollout_group: "saved_group",
      note: "saved note",
      // 10 Hz, the only rate acceleration runs at; the body leaves the default rate out
      control_hz: 10,
      chunk_accel: true,
    };
    const form = commandFormFromRun(saved)!;
    expect(form.chunkAccel).toBe(true);
    expect(form.inferenceMode).toBe("plain");
    const sent: Record<string, unknown> = { ...saved };
    delete sent.control_hz;
    expect(commandBody(form).body).toEqual(sent);
    // Missing (a run from before the switch) or not true is off.
    const old: Record<string, unknown> = { ...saved };
    delete old.chunk_accel;
    expect(commandFormFromRun(old)!.chunkAccel).toBeUndefined();
    expect(
      commandFormFromRun({ ...saved, chunk_accel: "yes" })!.chunkAccel,
    ).toBeUndefined();
  });

  test("the preview's control part says whether it is on, with its numbers", () => {
    const on = {
      enabled: true,
      experimental: true,
      speed_m_s: 0.25,
      tau_s: 0.2,
    };
    expect(
      readPreview(preview({ control: { hz: 10, chunk_accel: on } }))!
        .chunkAccel,
    ).toEqual({ speedMS: 0.25, tauS: 0.2, rule: "v1" });
    for (const control of [
      undefined,
      { hz: 10, chunk_accel: null },
      { hz: 10, chunk_accel: { ...on, enabled: false } },
      { hz: 10, chunk_accel: { ...on, speed_m_s: "fast" } },
    ])
      expect(
        readPreview(preview({ control }))!.chunkAccel,
        JSON.stringify(control),
      ).toBeUndefined();
  });

  test("the result line is worded in both languages with its numbers and rule", () => {
    for (const lang of ["en", "zh"] as const) {
      const text = sayText(
        {
          key: "marvel0.cmd.result.chunk_accel",
          params: { rule: "v2", speed: 0.25, tau: 0.2 },
        },
        catalogue(lang),
      );
      expect(text).toContain("0.25 m/s");
      expect(text).toContain("0.2");
      expect(text).toContain("v2");
      expect(text).not.toContain("{");
    }
  });

  test("rule v1 adds nothing to the body when it is chosen: a v1 command is the one it always was", () => {
    const body = commandBody(
      base({ ...plain, chunkAccel: true, chunkAccelRule: "v1" }),
    ).body;
    expect(body).toMatchObject({ chunk_accel: true });
    for (const field of [
      "chunk_accel_rule",
      "chunk_accel_lambda",
      "chunk_accel_alpha",
    ])
      expect(body).not.toHaveProperty(field);
  });

  test("the rule of a form is v3 until another is chosen, while the page offers v3", () => {
    expect([...CHUNK_ACCEL_RULES]).toEqual(["v1", "v2", "v3"]);
    expect(chunkAccelRuleDefault()).toBe("v3");
    expect(chunkAccelRuleOf(base(plain))).toBe("v3");
    expect(chunkAccelRuleOf(base({ ...plain, chunkAccelRule: "v1" }))).toBe(
      "v1",
    );
    expect(chunkAccelRuleOf(base({ ...plain, chunkAccelRule: "v2" }))).toBe(
      "v2",
    );
    // v1 and v2 run with plain inference only; the remembered choice comes back with plain
    expect(
      chunkAccelRuleOf(
        base({ rtc: true, inferenceMode: "rtc", chunkAccelRule: "v1" }),
      ),
    ).toBe("v3");
    expect(chunkAccelRulesFor(plain)).toEqual(["v1", "v2", "v3"]);
    expect(chunkAccelRulesFor({ rtc: true, inferenceMode: "rtc" })).toEqual([
      "v3",
    ]);
    expect(chunkAccelRulesFor({ rtc: false, inferenceMode: "vlash" })).toEqual([
      "v3",
    ]);
  });

  test("rule v2 is sent only with the switch on, plain inference and 10 Hz", () => {
    expect(
      commandBody(base({ ...plain, chunkAccel: true, chunkAccelRule: "v2" }))
        .body,
    ).toMatchObject({ chunk_accel: true, chunk_accel_rule: "v2" });
    for (const over of [
      { ...plain, chunkAccel: false },
      { ...plain },
      { ...plain, chunkAccel: true, hz: "20" },
    ]) {
      const body = commandBody(base({ ...over, chunkAccelRule: "v2" })).body;
      expect(body, JSON.stringify(over)).not.toHaveProperty("chunk_accel_rule");
    }
    // another inference mode: never rule v2 (it is rule v3 there)
    for (const over of [
      { rtc: true, inferenceMode: "rtc" as const, chunkAccel: true },
      { rtc: false, inferenceMode: "vlash" as const, chunkAccel: true },
    ]) {
      const body = commandBody(base({ ...over, chunkAccelRule: "v2" })).body;
      expect(body.chunk_accel_rule, JSON.stringify(over)).toBe("v3");
    }
  });

  test("rule v3 sends its rule and the alpha, and the lambda only when one is chosen", () => {
    const v3 = { ...plain, chunkAccel: true };
    const body = commandBody(base(v3)).body;
    expect(body).toMatchObject({
      chunk_accel: true,
      chunk_accel_rule: "v3",
      chunk_accel_alpha: 0.005,
    });
    // empty: the backend's constant applies (the 04:30 default is set there, once)
    expect(body).not.toHaveProperty("chunk_accel_lambda");
    expect(
      commandBody(base({ ...v3, chunkAccelLambda: "" })).body,
    ).not.toHaveProperty("chunk_accel_lambda");
    for (const lambda of CHUNK_ACCEL_V3_LAMBDAS)
      expect(
        commandBody(base({ ...v3, chunkAccelLambda: lambda })).body
          .chunk_accel_lambda,
      ).toBe(Number(lambda));
    for (const alpha of CHUNK_ACCEL_V3_ALPHAS)
      expect(
        commandBody(base({ ...v3, chunkAccelAlpha: alpha })).body
          .chunk_accel_alpha,
      ).toBe(Number(alpha));
    // the numbers are not sent without v3, nor without the switch
    for (const over of [
      { ...v3, chunkAccelRule: "v2" as const },
      { ...v3, chunkAccelRule: "v1" as const },
      { ...plain, chunkAccelRule: "v3" as const },
      { ...v3, hz: "20" },
    ])
      for (const field of ["chunk_accel_lambda", "chunk_accel_alpha"])
        expect(
          commandBody(
            base({ ...over, chunkAccelLambda: "2.5", chunkAccelAlpha: "0.01" }),
          ).body,
          JSON.stringify(over),
        ).not.toHaveProperty(field);
  });

  test("rule v3 keeps the mode's own fields: the RTC horizon, the VLASH overlap", () => {
    const rtc = commandBody(
      base({
        rtc: true,
        inferenceMode: "rtc",
        horizon: "5",
        chunkAccel: true,
        chunkAccelLambda: "2.5",
      }),
    ).body;
    expect(rtc).toMatchObject({
      rtc: true,
      inference_mode: "rtc",
      rtc_horizon: 5,
      chunk_accel: true,
      chunk_accel_rule: "v3",
      chunk_accel_lambda: 2.5,
    });
    const vlash = commandBody(
      base({
        rtc: false,
        inferenceMode: "vlash",
        vlashOverlapSteps: "3",
        chunkAccel: true,
      }),
    ).body;
    expect(vlash).toMatchObject({
      rtc: false,
      inference_mode: "vlash",
      vlash_overlap_steps: 3,
      chunk_accel: true,
      chunk_accel_rule: "v3",
    });
  });

  test("a lambda or an alpha the page does not offer is an error on its field, not a request", () => {
    const v3 = { ...plain, chunkAccel: true };
    const bad = commandBody(
      base({ ...v3, chunkAccelLambda: "1.5", chunkAccelAlpha: "0.02" }),
    );
    expect(bad.errors.chunk_accel_lambda).toEqual({
      key: "marvel0.err.chunk_accel_lambda_invalid",
    });
    expect(bad.errors.chunk_accel_alpha).toEqual({
      key: "marvel0.err.chunk_accel_alpha_invalid",
    });
    expect(bad.body).not.toHaveProperty("chunk_accel_lambda");
    expect(bad.body).not.toHaveProperty("chunk_accel_alpha");
    expect(commandBody(base(v3)).errors).toEqual({});
  });

  test("the alpha of the stack's anyspeedup arm controller replaces whatever the field held", () => {
    const v3 = { ...plain, chunkAccel: true };
    for (const [armAlpha, typed] of [
      [0.01, undefined],
      [0.01, "0.005"],
      [0.005, "0.01"],
      [0.005, undefined],
    ] as const) {
      const { body, errors } = commandBody(
        base({ ...v3, chunkAccelAlpha: typed }),
        10,
        armAlpha,
      );
      expect(errors, `${armAlpha} ${typed}`).toEqual({});
      expect(body.chunk_accel_alpha, `${armAlpha} ${typed}`).toBe(armAlpha);
    }
    // no arm alpha (the lab's controller, or not known): the field decides, as before
    expect(commandBody(base(v3), 10, null).body.chunk_accel_alpha).toBe(0.005);
    expect(
      commandBody(base({ ...v3, chunkAccelAlpha: "0.01" }), 10, null).body
        .chunk_accel_alpha,
    ).toBe(0.01);
    // and no alpha at all for a rule that has none
    expect(
      commandBody(
        base({ ...plain, chunkAccel: true, chunkAccelRule: "v2" }),
        10,
        0.01,
      ).body,
    ).not.toHaveProperty("chunk_accel_alpha");
    expect(commandBody(base(plain), 10, 0.01).body).not.toHaveProperty(
      "chunk_accel_alpha",
    );
  });

  test("an arm alpha the page has no tuning for is an error on the alpha field", () => {
    const { body, errors } = commandBody(
      base({ ...plain, chunkAccel: true }),
      10,
      0.02,
    );
    expect(errors.chunk_accel_alpha).toEqual({
      key: "marvel0.err.chunk_accel_alpha_arm_mismatch",
      params: { alpha: "0.02" },
    });
    expect(body).not.toHaveProperty("chunk_accel_alpha");
  });

  test("a saved v3 run round-trips into the form and back into the same body, for each mode", () => {
    for (const mode of ["plain", "rtc", "vlash"] as const) {
      const saved = {
        model_id: "pi05_fr3_all_step49999",
        prompt: "pick the eggplant in the blue plate",
        max_steps: 500,
        eval_num: 5,
        min_z: 0.025,
        rtc: mode === "rtc",
        inference_mode: mode,
        vlash_overlap_steps: 2,
        rtc_horizon: 7,
        rollout_group: "saved_group",
        note: "saved note",
        control_hz: 10,
        chunk_accel: true,
        chunk_accel_rule: "v3",
        chunk_accel_lambda: 2,
        chunk_accel_alpha: 0.01,
      };
      const form = commandFormFromRun(saved)!;
      expect(form.chunkAccelRule).toBe("v3");
      // 2 is the offer "2.0"
      expect(form.chunkAccelLambda).toBe("2.0");
      expect(form.chunkAccelAlpha).toBe("0.01");
      const sent: Record<string, unknown> = { ...saved };
      delete sent.control_hz;
      if (mode !== "vlash") delete sent.vlash_overlap_steps;
      expect(commandBody(form).body, mode).toEqual(sent);
    }
    // numbers the page does not offer, or none (a v1 or v2 run), restore nothing
    const form = commandFormFromRun({
      model_id: "m",
      prompt: "p",
      max_steps: 1,
      eval_num: 1,
      min_z: 0.05,
      rtc: false,
      rtc_horizon: 6,
      rollout_group: "g",
      note: "n",
      control_hz: 10,
      chunk_accel: true,
      chunk_accel_rule: "v3",
      chunk_accel_lambda: 1.5,
      chunk_accel_alpha: null,
    })!;
    expect(form.chunkAccelLambda).toBeUndefined();
    expect(form.chunkAccelAlpha).toBeUndefined();
  });

  test("a saved run with the switch on keeps the rule it ran with: the v3 default never turns a v1 run into v3", () => {
    const saved = {
      model_id: "m",
      prompt: "p",
      max_steps: 1,
      eval_num: 1,
      min_z: 0.05,
      rtc: false,
      inference_mode: "plain",
      rtc_horizon: 6,
      rollout_group: "g",
      note: "n",
      control_hz: 10,
      chunk_accel: true,
    };
    expect(commandFormFromRun(saved)!.chunkAccelRule).toBe("v1");
    expect(chunkAccelRuleOf(commandFormFromRun(saved)!)).toBe("v1");
    // a run with the switch off remembers no rule: turning it on afterwards opens on the default
    const off = commandFormFromRun({ ...saved, chunk_accel: false })!;
    expect(off.chunkAccelRule).toBeUndefined();
    expect(chunkAccelRuleOf(off)).toBe("v3");
  });

  test("a changed rule changes the request key", () => {
    const on = { ...plain, chunkAccel: true };
    expect(
      bodyKey(commandBody(base({ ...on, chunkAccelRule: "v2" })).body),
    ).not.toBe(bodyKey(commandBody(base(on)).body));
  });

  test("a saved v2 run round-trips into the form and back into the same body; a run without the rule is v1", () => {
    const saved = {
      model_id: "pi05_fr3_all_step49999",
      prompt: "pick the eggplant in the blue plate",
      max_steps: 500,
      eval_num: 5,
      min_z: 0.025,
      rtc: false,
      inference_mode: "plain",
      rtc_horizon: 7,
      rollout_group: "saved_group",
      note: "saved note",
      control_hz: 10,
      chunk_accel: true,
      chunk_accel_rule: "v2",
    };
    const form = commandFormFromRun(saved)!;
    expect(form.chunkAccelRule).toBe("v2");
    const sent: Record<string, unknown> = { ...saved };
    delete sent.control_hz;
    expect(commandBody(form).body).toEqual(sent);
    // a run from before the rule, a v1 run and anything else are v1
    const old: Record<string, unknown> = { ...saved };
    delete old.chunk_accel_rule;
    for (const run of [
      old,
      { ...saved, chunk_accel_rule: "v1" },
      { ...saved, chunk_accel_rule: "V2" },
    ]) {
      const back = commandFormFromRun(run)!;
      expect(chunkAccelRuleOf(back)).toBe("v1");
      expect(commandBody(back).body).not.toHaveProperty("chunk_accel_rule");
    }
  });

  test("the preview's control part of rule v3 says the mode, the lambda and the alpha", () => {
    const on = {
      enabled: true,
      experimental: true,
      rule: "v3",
      mode: "vlash",
      lambda_max: 2.5,
      alpha: 0.01,
    };
    const read = (chunk_accel: Record<string, unknown>) =>
      readPreview(preview({ control: { hz: 10, chunk_accel } }))!.chunkAccel;
    expect(read(on)).toEqual({
      rule: "v3",
      mode: "vlash",
      lambdaMax: 2.5,
      alpha: 0.01,
    });
    // a part that lacks a number or the mode, or is not enabled, is no answer (v3 has no ramp speed to fall back on)
    for (const broken of [
      { ...on, lambda_max: "fast" },
      { ...on, alpha: null },
      { ...on, mode: "fast" },
      { ...on, enabled: false },
    ])
      expect(read(broken), JSON.stringify(broken)).toBeUndefined();
  });

  test("the result line and the warnings of rule v3 are worded in both languages with their numbers", () => {
    for (const lang of ["en", "zh"] as const) {
      const line = sayText(
        {
          key: "marvel0.cmd.result.chunk_accel_v3",
          params: { mode: "RTC", lambda: 2.5, alpha: 0.01 },
        },
        catalogue(lang),
      );
      expect(line).toContain("2.5");
      expect(line).toContain("0.01");
      expect(line).toContain("RTC");
      expect(line).not.toContain("{");
    }
    const main = warningSay({
      code: "chunk_accel_v3",
      text: "English",
      params: { mode: "rtc", lambda_max: "2.0" },
    });
    const alpha = warningSay({
      code: "chunk_accel_v3_alpha",
      text: "English",
      params: { alpha: "0.005" },
    });
    expect(main.say?.key).toBe("marvel0.cmd.warning.chunk_accel_v3");
    expect(alpha.say?.key).toBe("marvel0.cmd.warning.chunk_accel_v3_alpha");
    for (const lang of ["en", "zh"] as const) {
      const a = sayText(main.say!, catalogue(lang));
      const b = sayText(alpha.say!, catalogue(lang));
      expect(a).toContain("2.0");
      expect(a).toContain("rtc");
      expect(b).toContain("0.005");
      expect(b).toContain("set_clip.py --show");
      expect(a + b).not.toMatch(/\{|marvel0\./);
    }
  });

  test("the preview's control part names the rule: v2 when it says so, else v1", () => {
    const on = {
      enabled: true,
      experimental: true,
      speed_m_s: 0.25,
      tau_s: 0.2,
    };
    const rule = (chunk_accel: Record<string, unknown>) =>
      readPreview(preview({ control: { hz: 10, chunk_accel } }))!.chunkAccel
        ?.rule;
    expect(rule({ ...on, rule: "v2" })).toBe("v2");
    expect(rule({ ...on, rule: "v1" })).toBe("v1");
    // an answer from before the rule ran v1
    expect(rule(on)).toBe("v1");
    expect(rule({ ...on, rule: "v9" })).toBe("v1");
  });

  test("the warning of rule v2 is worded in both languages", () => {
    const said = warningSay({ code: "chunk_accel_rule_v2", text: "English" });
    expect(said.say).toEqual({
      key: "marvel0.cmd.warning.chunk_accel_rule_v2",
    });
    for (const lang of ["en", "zh"] as const)
      expect(sayText(said.say!, catalogue(lang))).not.toMatch(/^marvel0\./);
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

  test("with in-chunk acceleration the default group is the accel one the backend files it under", () => {
    // review 2026-10-31: the page showed <id>_demo while the backend used <id>_accel_demo, so the start phrase
    // never matched and every start from the page was refused
    const v1 = { chunkAccel: true, chunkAccelRule: "v1" as const };
    expect(placeholders(base({ rtc: false, ...v1 }), models, site).group).toBe(
      "pi05_fr3_all_step49999_accel_demo",
    );
    // v1 runs with plain inference only: under RTC the rule is v3, filed under its own group
    expect(placeholders(base({ rtc: true, ...v1 }), models, site).group).toBe(
      "pi05_fr3_all_step49999_rtc_accel3_demo",
    );
    // and a form at another rate than 10 Hz sends no acceleration, so it is filed as without
    expect(
      placeholders(base({ rtc: false, hz: "20", ...v1 }), models, site).group,
    ).toBe("pi05_fr3_all_step49999_demo");
  });

  test("with rule v3 the default group is the one of its mode, as the backend files it", () => {
    const on = { chunkAccel: true };
    const group = (over: Partial<CommandForm>) =>
      placeholders(base({ ...on, ...over }), models, site).group;
    expect(group({ rtc: false, inferenceMode: "plain" })).toBe(
      "pi05_fr3_all_step49999_accel3_demo",
    );
    expect(group({ rtc: true, inferenceMode: "rtc" })).toBe(
      "pi05_fr3_all_step49999_rtc_accel3_demo",
    );
    expect(group({ rtc: false, inferenceMode: "vlash" })).toBe(
      "pi05_fr3_all_step49999_vlash_accel3_demo",
    );
    // without the switch, the mode's own group
    expect(group({ chunkAccel: false, rtc: true })).toBe(
      "pi05_fr3_all_step49999_rtc_demo",
    );
    // the page repeats the backend's naming: it holds for every model the backend's own sample lists
    const rows = deploySamples.models.models;
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      const names = row.rollout_group_default as Record<string, string>;
      const mine = (over: Partial<CommandForm>) =>
        placeholders(
          base({ ...on, modelId: row.id, ...over }),
          {
            models: [{ ...models.models[0], id: row.id }],
            skipped: [],
            problems: [],
            defaults: models.defaults,
          },
          site,
        ).group;
      expect(mine({ rtc: false, inferenceMode: "plain" })).toBe(names.accel3);
      expect(mine({ rtc: true, inferenceMode: "rtc" })).toBe(names.rtc_accel3);
      expect(mine({ rtc: false, inferenceMode: "vlash" })).toBe(
        names.vlash_accel3,
      );
    }
  });

  test("with rule v2 the default group is the v2 one the backend files it under", () => {
    const v2 = { rtc: false, chunkAccel: true, chunkAccelRule: "v2" as const };
    expect(placeholders(base(v2), models, site).group).toBe(
      "pi05_fr3_all_step49999_accel_v2_demo",
    );
    // the rule alone, without the switch, files nothing apart
    expect(
      placeholders(base({ ...v2, chunkAccel: false }), models, site).group,
    ).toBe("pi05_fr3_all_step49999_demo");
    expect(
      placeholders(base({ ...v2, chunkAccelRule: "v1" }), models, site).group,
    ).toBe("pi05_fr3_all_step49999_accel_demo");
  });

  test("a saved form opened for editing loses only the group and note the server filled in", () => {
    // review 2026-11-01: they were kept as typed text, so a rule or mode chosen next was filed under the old default
    const id = "pi05_fr3_all_step49999";
    const v2 = {
      rtc: false,
      inferenceMode: "plain" as const,
      chunkAccel: true,
      chunkAccelRule: "v2" as const,
    };
    const filled = base({
      ...v2,
      group: `${id}_accel_v2_demo`,
      note: `marvel0 page: ${id} chunk accel experimental rule=v2 horizon=7`,
    });
    expect(editableSavedForm(filled, models, site)).toEqual({
      ...filled,
      group: "",
      note: "",
    });
    // v1's default is not v2's: a v2 run filed under it was typed so
    const typed = base({ ...v2, group: `${id}_accel_demo`, note: "my note" });
    expect(editableSavedForm(typed, models, site)).toEqual(typed);
    expect(
      editableSavedForm(
        base({ rtc: true, group: `${id}_rtc_demo` }),
        models,
        site,
      ).group,
    ).toBe("");
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
      needs: {
        modelId: "pi05_fr3_all_step49999",
        rtc: true,
        inferenceMode: "rtc",
      },
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
    expect(match(ready(), sft, false).kind).toBe("other_mode");
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
