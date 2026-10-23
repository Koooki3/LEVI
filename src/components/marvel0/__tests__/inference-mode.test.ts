import { expect, test } from "bun:test";
import { readModels, readSite } from "../catalog-logic";
import {
  commandBody,
  commandFormFromRun,
  initialCommandForm,
  placeholders,
  readPreview,
  serverMatch,
} from "../command-logic";
import {
  deployBody,
  initialForm,
  readinessRows,
  readDeploy,
} from "../deploy-logic";
import { inferenceMode, validOverlap } from "../inference-mode";
import { effectiveSelection, NO_CHOICE } from "../selection";
import { modelsAnswer, siteAnswer } from "./catalog-fixtures";
import { deployInfo, ready } from "./deploy-fixtures";
import { preview } from "./command-fixtures";
const models = readModels(modelsAnswer())!;
const site = readSite(siteAnswer())!;
const model = models.models[0];
test("explicit VLASH overrides the legacy RTC bit and is locked to the running server", () => {
  const status = readDeploy(
    ready({
      deploy: deployInfo({
        inference_mode: "vlash",
        rtc: true,
        vlash_overlap_steps: 3,
      }),
    }),
  )!;
  expect(status.deploy?.rtc).toBe(false);
  const selection = effectiveSelection(
    models,
    { ...NO_CHOICE, inferenceMode: "rtc" },
    status,
  )!;
  expect(selection).toMatchObject({
    inferenceMode: "vlash",
    rtc: false,
    vlashOverlapSteps: "3",
    source: "server",
  });
  expect(
    serverMatch(
      { modelId: model.id, rtc: false, inferenceMode: "vlash" },
      status,
    ).good,
  ).toBe(true);
  for (const mode of ["plain", "rtc"] as const)
    expect(
      serverMatch(
        { modelId: model.id, rtc: mode === "rtc", inferenceMode: mode },
        status,
      ).kind,
    ).toBe("other_mode");
});
test("VLASH travels through deployment, preview and saved run forms without becoming plain", () => {
  const form = {
    ...initialCommandForm(models, site),
    inferenceMode: "vlash" as const,
    rtc: true,
    vlashOverlapSteps: "3",
  };
  const body = commandBody(form).body;
  expect(body).toMatchObject({
    inference_mode: "vlash",
    rtc: false,
    vlash_overlap_steps: 3,
  });
  expect(
    commandFormFromRun({
      ...body,
      rtc_horizon: 6,
      rollout_group: "g",
      note: "",
      control_hz: 10,
    }),
  ).toMatchObject({
    inferenceMode: "vlash",
    rtc: false,
    vlashOverlapSteps: "3",
  });
  expect(
    deployBody(
      "req",
      {
        ...initialForm(models, site),
        inferenceMode: "vlash",
        vlashOverlapSteps: "3",
      },
      model,
      "confirm",
    ),
  ).toMatchObject({
    inference_mode: "vlash",
    rtc: false,
    vlash_overlap_steps: 3,
  });
  expect(
    readPreview(
      preview({
        server_requirements: {
          model_id: model.id,
          rtc: false,
          inference_mode: "vlash",
        },
      }),
    )?.needs.inferenceMode,
  ).toBe("vlash");
  expect(placeholders(form, models, site).group).toContain("_vlash_demo");
});
test("legacy RTC fallback and VLASH-only positive integer overlap", () => {
  expect(inferenceMode(undefined, true)).toBe("rtc");
  expect(inferenceMode(undefined, false)).toBe("plain");
  for (const value of ["0", "-1", "1.2", "no", "Infinity"])
    expect(validOverlap(value)).toBe(false);
  const form = {
    ...initialCommandForm(models, site),
    inferenceMode: "vlash" as const,
  };
  expect(commandBody(form).body.vlash_overlap_steps).toBe(2);
  expect(
    commandBody({ ...form, vlashOverlapSteps: "1.5" }).errors
      .vlash_overlap_steps,
  ).toBeDefined();
  for (const mode of ["plain", "rtc"] as const)
    expect(
      commandBody({ ...form, inferenceMode: mode, vlashOverlapSteps: "bad" })
        .body,
    ).not.toHaveProperty("vlash_overlap_steps");
});

test("VLASH readiness requires its own protocol confirmation, not RTC warmup", () => {
  const status = readDeploy(
    ready({
      deploy: deployInfo({ inference_mode: "vlash", rtc: false }),
      readiness: { norm_stats: true, listening: true, vlash_enabled: false },
    }),
  )!;
  const rows = readinessRows(status);
  expect(rows.find((row) => row.id === "vlash_enabled")?.value).toBe(false);
  expect(
    rows.some((row) => row.id === "rtc_enabled" || row.id === "rtc_warmup"),
  ).toBe(false);
  expect(
    readinessRows(
      readDeploy(
        ready({
          deploy: deployInfo({ inference_mode: "vlash", rtc: false }),
          readiness: { vlash_enabled: true },
        }),
      )!,
    ).find((row) => row.id === "vlash_enabled")?.value,
  ).toBe(true);
});
