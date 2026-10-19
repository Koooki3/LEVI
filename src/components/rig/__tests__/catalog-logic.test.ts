import { describe, expect, test } from "bun:test";
import {
  modelOptionText,
  readModels,
  readSite,
  skippedSay,
  warningSay,
} from "../catalog-logic";
import { cfgModel, model, modelsAnswer, siteAnswer } from "./catalog-fixtures";

describe("readModels", () => {
  test("reads the models, why others are left out and the defaults", () => {
    const view = readModels(modelsAnswer())!;
    expect(view.models.map((m) => m.id)).toEqual([
      "pi05_fr3_all_step49999",
      "recap_cfg_r2_best_step14300_jax",
    ]);
    expect(view.models[0]).toEqual({
      id: "pi05_fr3_all_step49999",
      config: "pi05_fr3_all_state",
      cfgCapable: false,
      restrictedPrompts: false,
      verified: true,
      step: 49999,
      sizeBytes: 12436487155,
      warnings: ["training_config_name_differs_from_serving_config"],
      groupRtc: "pi05_fr3_all_step49999_rtc_demo",
      groupPlain: "pi05_fr3_all_step49999_demo",
    });
    expect(view.models[1]).toMatchObject({ cfgCapable: true, verified: false });
    expect(view.skipped).toEqual([
      { id: "recap_global_step_10000", reason: "not_deployable" },
      { id: "reset_r1_step20000", reason: "excluded_by_site" },
    ]);
    expect(view.problems).toEqual([]);
    expect(view.defaults).toEqual({ rtc: true, fraction: 0.22, cfgScale: 1 });
  });

  test("a model takes only the known instructions when the server says it does", () => {
    const view = readModels(
      modelsAnswer({
        models: [
          model({ restricted_prompts: true }),
          model({ id: "b", restricted_prompts: "yes" }),
        ],
      }),
    )!;
    expect(view.models.map((m) => m.restrictedPrompts)).toEqual([true, false]);
  });

  test("a model is unverified unless the server says it is verified", () => {
    const view = readModels(
      modelsAnswer({
        models: [
          model({ verified: "yes" }),
          model({ id: "b", verified: undefined }),
        ],
      }),
    )!;
    expect(view.models.map((m) => m.verified)).toEqual([false, false]);
  });

  test("a model without an id or config is left out, not guessed", () => {
    const view = readModels(
      modelsAnswer({
        models: [model({ id: "" }), model({ config: 3 }), model(), "x", null],
      }),
    )!;
    expect(view.models).toHaveLength(1);
  });

  test("a wrong schema, a missing list or a non-object is no answer", () => {
    expect(
      readModels(modelsAnswer({ schema: "levi.rig.models.v2" })),
    ).toBeNull();
    expect(readModels(modelsAnswer({ models: "none" }))).toBeNull();
    expect(readModels(null)).toBeNull();
    expect(readModels([])).toBeNull();
  });

  test("defaults fall back to RTC on and no fraction", () => {
    const view = readModels(modelsAnswer({ defaults: {} }))!;
    expect(view.defaults).toEqual({
      rtc: true,
      fraction: null,
      cfgScale: null,
    });
  });
});

describe("readSite", () => {
  test("reads what the forms need", () => {
    const site = readSite(siteAnswer())!;
    expect(site.port).toBe(8000);
    expect(site.homeZ).toBeCloseTo(0.235951);
    expect(site.minZ).toEqual({ floor: 0.025, default: 0.025 });
    expect(site.caps.maxSteps).toEqual({ default: 500, cap: 3000 });
    expect(site.caps.evalNum).toEqual({ default: 5, cap: 50 });
    expect(site.caps.rtcHorizon).toEqual({ default: 6, max: 8 });
    expect(site.caps.openLoop).toEqual({ default: 6, max: 10 });
    expect(site.caps.promptMaxChars).toBe(300);
    expect(site.caps.noteMaxChars).toBe(200);
    expect(site.caps.cfgScale).toEqual({ default: 1, max: 1.5 });
    expect(site.prompts.default).toBe("pick the eggplant in the blue plate");
    expect(site.prompts.known).toHaveLength(3);
    expect(site.prompts.restricted).toEqual([
      "pick screws of same size into the empty slot of screw box",
      "stack the plates of same color together",
    ]);
    expect(site.prompts.restrictedPrefixes).toEqual(["pi05_fr3_sp"]);
    expect(site.fixedFlags).toContain("--max-xy=0.85");
    expect(site.gpu).toEqual({
      presets: [0.22, 0.3, 0.45],
      marginMib: 1024,
    });
    expect(site.unit).toEqual({ stopTimeoutS: 90, startTimeoutS: 600 });
  });

  test("a site view missing a number the forms rely on is no answer", () => {
    expect(readSite(siteAnswer({ min_z: {} }))).toBeNull();
    expect(readSite(siteAnswer({ min_z: { default: 0.025 } }))).toBeNull();
    expect(readSite(siteAnswer({ min_z: { floor: 0.025 } }))).toBeNull();
    expect(readSite(siteAnswer({ caps: {} }))).toBeNull();
    expect(readSite(siteAnswer({ home: { pose: [1, 2] } }))).toBeNull();
    expect(readSite(siteAnswer({ schema: "other" }))).toBeNull();
    expect(readSite("x")).toBeNull();
  });
});

describe("words", () => {
  test("the option of a model names the checkpoint, its config and the missing check", () => {
    const [verified, unverified] = readModels(modelsAnswer())!.models;
    expect(modelOptionText(verified, (k) => k)).toBe(
      "pi05_fr3_all_step49999 · pi05_fr3_all_state",
    );
    expect(modelOptionText(unverified, (k) => `<${k}>`)).toBe(
      "recap_cfg_r2_best_step14300_jax · pi05_fr3_all_state_cfg · <rig.deploy.model.unverified>",
    );
  });

  test("every reason a checkpoint is left out is worded, and an unknown one is shown as written", () => {
    for (const reason of [
      "not_deployable",
      "not_jax",
      "role_not_forward",
      "config_unknown",
      "no_toplevel_norm_stats",
      "excluded_by_site",
      "unsafe_name",
      "name_too_long",
    ])
      expect(skippedSay(reason)).toMatchObject({
        say: { key: `rig.deploy.skipped.${reason}` },
      });
    expect(skippedSay("brand_new")).toEqual({ say: null, text: "brand_new" });
  });

  test("a model warning is worded by its code; a code with detail keeps the code part", () => {
    expect(warningSay("size_is_a_lower_bound")).toMatchObject({
      say: { key: "rig.deploy.warning.size_is_a_lower_bound" },
    });
    expect(warningSay("role_withheld:reset_hint:a,b")).toMatchObject({
      say: { key: "rig.deploy.warning.role_withheld" },
    });
    expect(warningSay("something_new")).toEqual({
      say: null,
      text: "something_new",
    });
  });

  test("the cfg model fixture is a cfg model", () => {
    expect(cfgModel().cfg_capable).toBe(true);
  });
});
