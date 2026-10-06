import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  DEFAULT_RESET_STATE,
  DIRECTION_HINTS,
  DIRECTION_LABELS,
  MAX_RELEASE_HINTS,
  MAX_RELEASE_LABELS,
  ON_INELIGIBLE_HINTS,
  ON_INELIGIBLE_LABELS,
  RELEASE_CLASS_HINTS,
  RELEASE_CLASS_LABELS,
  RELEASE_REASON_LABELS,
  analysisPayload,
  clampSettledRows,
  isResetReason,
  needsCapture,
  parseBridges,
  previewText,
  releaseReasonLabel,
  resetPayload,
  shortKey,
  sortedClasses,
  sortedReasons,
  templateProblem,
  type ResetState,
} from "../reset";
import { resetFormProblem } from "../reset-panel";
import { REASON_LABELS } from "../types";

const on = (patch: Partial<ResetState> = {}): ResetState => ({
  ...DEFAULT_RESET_STATE,
  direction: "forward_and_reset",
  ...patch,
});

describe("resetPayload", () => {
  test("forward only sends nothing", () => {
    expect(resetPayload(DEFAULT_RESET_STATE, "lerobot_v21")).toBeUndefined();
  });

  test("formats without reset send nothing even when a direction is set", () => {
    expect(resetPayload(on(), "recap_value")).toBeUndefined();
    expect(resetPayload(on(), "raw_capture")).toBeUndefined();
  });

  test("the defaults are the backend defaults", () => {
    expect(
      resetPayload(on({ direction: "reset_only" }), "lerobot_v21"),
    ).toEqual({
      direction: "reset_only",
      task_template: "Reset: {task}",
      action_contract: "fr3-robotiq@1",
      max_release: "in_reach",
      on_ineligible: "exclude",
    });
  });

  test("optional fields are sent only when set", () => {
    const sent = resetPayload(
      on({
        maxRelease: "in_place",
        onIneligible: "partial",
        reviewModel: " vlm-local ",
        releaseCamera: "observation.images.wrist",
        bridgesText: "a = b\n\n# note\nc=d",
      }),
      "lerobot_v21",
    );
    expect(sent).toMatchObject({
      max_release: "in_place",
      on_ineligible: "partial",
      review_model: "vlm-local",
      release_camera: "observation.images.wrist",
      bridges: [
        { source: "a", record: "b" },
        { source: "c", record: "d" },
      ],
    });
  });

  test("the grasp and settling options are sent only when they differ from the default", () => {
    const plain = resetPayload(on(), "lerobot_v21");
    expect(plain).not.toHaveProperty("allow_no_grasp");
    expect(plain).not.toHaveProperty("min_settled_rows");
    expect(
      resetPayload(
        on({ allowNoGrasp: true, minSettledRows: 1 }),
        "lerobot_v21",
      ),
    ).toMatchObject({ allow_no_grasp: true, min_settled_rows: 1 });
    expect(
      resetPayload(on({ minSettledRows: 2 }), "lerobot_v21"),
    ).not.toHaveProperty("min_settled_rows");
    expect(
      resetPayload(on({ minSettledRows: 99 }), "lerobot_v21")?.min_settled_rows,
    ).toBe(6);
    expect(analysisPayload(on({ minSettledRows: 3 })).min_settled_rows).toBe(3);
  });

  test("the settled-frames count is kept to 1..6", () => {
    expect(clampSettledRows(0)).toBe(1);
    expect(clampSettledRows(3.4)).toBe(3);
    expect(clampSettledRows(7)).toBe(6);
    expect(clampSettledRows(NaN)).toBe(2);
  });

  test("an empty contract falls back to the default", () => {
    expect(
      resetPayload(on({ actionContract: "  " }), "lerobot_v21")
        ?.action_contract,
    ).toBe("fr3-robotiq@1");
  });

  test("the reversibility check needs a reset direction", () => {
    expect(analysisPayload(DEFAULT_RESET_STATE).direction).toBe("reset_only");
    expect(analysisPayload(on()).direction).toBe("forward_and_reset");
  });
});

describe("template", () => {
  test("must hold {task} exactly once and no other braces", () => {
    expect(templateProblem("Reset: {task}")).toBeNull();
    expect(templateProblem("Put back: {task}.")).toBeNull();
    expect(templateProblem("Reset")).toContain("must contain {task}");
    expect(templateProblem("")).toContain("must contain {task}");
    expect(templateProblem("{task} / {task}")).toContain("only once");
    expect(templateProblem("{task} {other}")).toContain("braces");
    expect(templateProblem("{ {task}")).toContain("braces");
    expect(templateProblem("x".repeat(400) + "{task}")).toContain("too long");
  });

  test("the preview fills in the task and is empty for a bad template", () => {
    expect(previewText("Reset: {task}", "pick the screw")).toBe(
      "Reset: pick the screw",
    );
    expect(previewText("Reset", "pick the screw")).toBe("");
  });

  test("the task text is inserted as it is", () => {
    expect(previewText("Reset: {task}", "a {b} $& c")).toBe(
      "Reset: a {b} $& c",
    );
  });
});

describe("parseBridges", () => {
  test("reads one pair per line, skipping blanks and comments", () => {
    expect(parseBridges("a = b\n\n  # skip\n c=d \n")).toEqual({
      bridges: [
        { source: "a", record: "b" },
        { source: "c", record: "d" },
      ],
      invalid: [],
    });
  });

  test("reports the 1-based line numbers that are not a pair", () => {
    const parsed = parseBridges("a = b\nnoequals\n= x\ny =\nok=fine");
    expect(parsed.invalid).toEqual([2, 3, 4]);
    expect(parsed.bridges).toHaveLength(2);
  });

  test("a forward key given twice keeps the last record", () => {
    expect(parseBridges("a = b\na = c").bridges).toEqual([
      { source: "a", record: "c" },
    ]);
  });

  test("only the first = splits", () => {
    expect(parseBridges("a = b=c").bridges).toEqual([
      { source: "a", record: "b=c" },
    ]);
  });
});

describe("resetFormProblem", () => {
  test("nothing blocks a forward-only export, whatever the fields hold", () => {
    expect(
      resetFormProblem({ ...DEFAULT_RESET_STATE, taskTemplate: "bad" }),
    ).toBeNull();
  });

  test("a bad template or bridge line blocks a reset export", () => {
    expect(resetFormProblem(on())).toBeNull();
    expect(resetFormProblem(on({ taskTemplate: "bad" }))).toContain("template");
    expect(resetFormProblem(on({ bridgesText: "oops" }))).toContain(
      "recorded-stretch",
    );
  });
});

describe("labels and order", () => {
  test("release classes are listed best first and empty ones are left out", () => {
    expect(
      sortedClasses({ unknown: 1, in_place: 5, escaped: 2, in_reach: 0 }),
    ).toEqual([
      ["in_place", 5],
      ["escaped", 2],
      ["unknown", 1],
    ]);
  });

  test("reasons go by count, then by code", () => {
    expect(
      sortedReasons({
        reset_release_unknown: 1,
        reset_forward_failed: 3,
        reset_action_contract: 1,
      }).map(([code]) => code),
    ).toEqual([
      "reset_forward_failed",
      "reset_action_contract",
      "reset_release_unknown",
    ]);
  });

  test("free-text camera errors share one label; unknown codes stay as given", () => {
    expect(releaseReasonLabel("release_camera_unreadable: bad moov atom")).toBe(
      RELEASE_REASON_LABELS.release_camera_unreadable,
    );
    expect(releaseReasonLabel("low_texture")).toBe(
      RELEASE_REASON_LABELS.low_texture,
    );
    expect(releaseReasonLabel("brand_new")).toBe("brand_new");
    expect(releaseReasonLabel("not_settled")).toBe(
      RELEASE_REASON_LABELS.not_settled,
    );
    expect(RELEASE_REASON_LABELS.review_failed).toBeTruthy();
  });

  test("short keys keep the end of a path", () => {
    expect(shortKey("demo_1")).toBe("demo_1");
    expect(shortKey("/data/run/2026-10-01/demo_0007")).toBe(
      "2026-10-01/demo_0007",
    );
    expect(shortKey("/a/" + "x".repeat(60), 20)).toHaveLength(20);
  });

  test("reset reason codes and the capture request hint", () => {
    expect(isResetReason("reset_unreadable")).toBe(true);
    expect(isResetReason("heldout")).toBe(false);
    expect(needsCapture(undefined)).toBe(false);
    expect(needsCapture({ reset_release_in_reach: 2 })).toBe(false);
    expect(needsCapture({ reset_release_escaped: 1 })).toBe(true);
    expect(needsCapture({ reset_release_unknown: 1 })).toBe(true);
  });
});

describe("catalogue", () => {
  const codes = [
    "reset_forward_failed",
    "reset_forward_unlabeled",
    "reset_already_reset",
    "reset_action_contract",
    "reset_release_escaped",
    "reset_release_unknown",
    "reset_release_in_reach",
    "reset_unreadable",
    "reset_bridge_missing",
    "reset_bridge_contract",
    "reset_bridge_start_mismatch",
    "reset_bridge_no_grasp",
    "reset_bridge_never_reaches_anchor",
    "reset_bridge_nothing_held",
    "reset_bridge_visual_mismatch",
    "reset_bridge_visual_unchecked",
    "reset_no_grasp",
    "reset_video_rows",
    "reset_bridge_cameras",
    "reset_write_error",
  ];

  test("every reset exclusion code has a label", () => {
    for (const code of codes) expect(REASON_LABELS[code]).toBeTruthy();
  });

  test("the stage name and every label exist in both languages", () => {
    const keys = [
      "Reverse for reset",
      ...codes.map((c) => REASON_LABELS[c]),
      ...Object.values(DIRECTION_LABELS),
      ...Object.values(DIRECTION_HINTS),
      ...Object.values(MAX_RELEASE_LABELS),
      ...Object.values(MAX_RELEASE_HINTS),
      ...Object.values(ON_INELIGIBLE_LABELS),
      ...Object.values(ON_INELIGIBLE_HINTS),
      ...Object.values(RELEASE_CLASS_LABELS),
      ...Object.values(RELEASE_CLASS_HINTS),
      ...Object.values(RELEASE_REASON_LABELS),
      // The template problems and the form problems of the export.
      "The template must contain {task}.",
      "Use {task} only once in the template.",
      "Only {task} may appear in braces.",
      "The template is too long (400 characters).",
      "Fix the reset instruction template above.",
      "Fix the recorded-stretch lines above.",
    ];
    for (const key of keys) {
      expect((en as Record<string, string>)[key]).toBeTruthy();
      expect((zh as Record<string, string>)[key]).toBeTruthy();
    }
  });
});
