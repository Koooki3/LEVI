import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AnalysisResult, ResetPanel } from "../reset-panel";
import { DEFAULT_RESET_STATE, type ResetState } from "../reset";
import type { Recipe, ResetAnalysis } from "../types";

const recipe = {
  name: "r",
  tasks: [{ task: "pick the screw", count: null }],
} as unknown as Recipe;

function panel(state: ResetState) {
  return renderToStaticMarkup(
    createElement(ResetPanel, {
      state,
      onChange: () => {},
      recipe,
      cameras: {},
      cameraMap: {},
      firstTask: "pick the screw",
    }),
  );
}

describe("reset panel", () => {
  test("forward only shows the direction and no settings", () => {
    const html = panel(DEFAULT_RESET_STATE);
    expect(html).toContain("Data direction");
    expect(html).not.toContain("Reset instruction template");
    expect(html).not.toContain("Check reversibility");
  });

  test("a reset direction shows the settings, the preview and the check", () => {
    const html = panel({ ...DEFAULT_RESET_STATE, direction: "reset_only" });
    expect(html).toContain("Reset: pick the screw");
    expect(html).toContain("Worst release allowed");
    expect(html).toContain("does not filter out still frames");
    expect(html).toContain("Check reversibility");
    expect(html).toContain("Also reverse episodes without a grasp");
    expect(html).toContain("Frames in a row that show the object settled");
    expect(html).not.toContain("pg-pool-bad");
  });

  test("a bad template is explained next to the field", () => {
    const html = panel({
      ...DEFAULT_RESET_STATE,
      direction: "forward_and_reset",
      taskTemplate: "Reset",
    });
    expect(html).toContain("The template must contain {task}.");
    expect(html).toContain('aria-invalid="true"');
  });
});

describe("reversibility result", () => {
  const result: ResetAnalysis = {
    summary: {
      episodes: 3,
      reversible: 2,
      reasons: { reset_release_escaped: 1 },
      release_classes: { in_place: 2, escaped: 1 },
    },
    episodes: [
      {
        key: "/data/run/demo_0001",
        eligible: true,
        reason: null,
        scope: "full",
        releases: [{ row: 40, class: "in_place", reason: null }],
      },
      {
        key: "/data/run/demo_0002",
        eligible: true,
        reason: null,
        scope: "partial",
        releases: [],
      },
      {
        key: "/data/run/demo_0003",
        eligible: false,
        reason: "reset_release_escaped",
        releases: [{ row: 90, class: "escaped", reason: "low_texture" }],
      },
    ],
    selected: 87,
    analyzed: 3,
    profile: "reset-profile-test",
  };

  test("shows counts, reasons, one row per episode and the profile", () => {
    const html = renderToStaticMarkup(
      createElement(AnalysisResult, { result, stale: false }),
    );
    expect(html).toContain("Checked 3 of 87 selected episodes");
    expect(html).toContain("Partly reversible");
    expect(html).toContain("the object ended out of the gripper&#x27;s reach");
    expect(html).toContain("too little texture to follow the object");
    expect(html).toContain("run/demo_0003");
    expect(html).toContain("reset-profile-test");
    expect(html).toContain("not a guarantee");
    expect(html).toContain("about 0.5 s after opening the gripper");
    expect(html).not.toContain("changed since this check");
  });

  test("an out-of-date result says so", () => {
    const html = renderToStaticMarkup(
      createElement(AnalysisResult, { result, stale: true }),
    );
    expect(html).toContain("changed since this check");
  });
});
