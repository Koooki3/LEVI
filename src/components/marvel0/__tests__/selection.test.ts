import { describe, expect, test } from "bun:test";
import { readModels } from "../catalog-logic";
import { readDeploy } from "../deploy-logic";
import { NO_CHOICE, effectiveSelection, serverHolding } from "../selection";
import { cfgModel, modelsAnswer } from "./catalog-fixtures";
import { deployInfo, idle, ready, starting } from "./deploy-fixtures";

const SFT = "pi05_fr3_all_step49999";
const CFG = "recap_cfg_r2_best_step14300_jax";
const models = readModels(modelsAnswer())!;

describe("the page's one selected model", () => {
  test("before the model list is read there is none", () => {
    expect(effectiveSelection(null, NO_CHOICE, null)).toBeNull();
    expect(
      effectiveSelection(
        readModels(modelsAnswer({ models: [] })),
        NO_CHOICE,
        null,
      ),
    ).toBeNull();
  });

  test("nothing picked: the first model, with the site's RTC default", () => {
    expect(effectiveSelection(models, NO_CHOICE, readDeploy(idle()))).toEqual({
      modelId: SFT,
      rtc: true,
      inferenceMode: "rtc",
      vlashOverlapSteps: "2",
      cfgScale: null,
      source: "default",
    });
  });

  test("what is picked in the policy server card is the selection", () => {
    expect(
      effectiveSelection(
        models,
        { modelId: CFG, rtc: false, scale: "" },
        readDeploy(idle()),
      ),
    ).toEqual({
      modelId: CFG,
      rtc: false,
      inferenceMode: "plain",
      vlashOverlapSteps: "2",
      cfgScale: null,
      source: "choice",
    });
  });

  test("a pick that left the list falls back to the first model", () => {
    expect(
      effectiveSelection(
        models,
        { modelId: "gone", rtc: null, scale: "" },
        null,
      )?.source,
    ).toBe("default");
  });

  test("while this page's server starts or runs, what it runs is the selection, whatever was picked", () => {
    for (const status of [
      readDeploy(
        starting({ deploy: deployInfo({ model_id: CFG, rtc: false }) }),
      ),
      readDeploy(
        ready({
          deploy: deployInfo({
            model_id: CFG,
            rtc: false,
            cfg_guidance_scale: 1.5,
          }),
        }),
      ),
    ]) {
      expect(
        effectiveSelection(
          models,
          { modelId: SFT, rtc: true, scale: "" },
          status,
        ),
      ).toMatchObject({ modelId: CFG, rtc: false, source: "server" });
    }
    expect(
      effectiveSelection(
        models,
        NO_CHOICE,
        readDeploy(
          ready({
            deploy: deployInfo({ model_id: CFG, cfg_guidance_scale: 1.5 }),
          }),
        ),
      )?.cfgScale,
    ).toBe(1.5);
  });

  test("a server that is not this page's, or is gone, holds no model", () => {
    expect(serverHolding(readDeploy(idle()))).toBeNull();
    expect(serverHolding(null)).toBeNull();
    expect(
      serverHolding(readDeploy(idle({ state: "foreign_listener" }))),
    ).toBeNull();
    expect(
      effectiveSelection(
        models,
        { modelId: CFG, rtc: null, scale: "" },
        readDeploy(idle()),
      )?.source,
    ).toBe("choice");
  });

  test("a running model that the list does not offer does not override the pick", () => {
    const status = readDeploy(
      ready({ deploy: deployInfo({ model_id: "not_in_list" }) }),
    );
    expect(effectiveSelection(models, NO_CHOICE, status)?.source).toBe(
      "default",
    );
  });
});

void cfgModel;
