import { describe, expect, test } from "bun:test";
import { Euler, Quaternion } from "three";
import type { PoseFields, PreviewPose } from "../preview-state";
import {
  cloneTeleopDraft,
  cloneToolDraft,
  DEFAULT_TOOL_DRAFT,
  EMPTY_TELEOP_DRAFT,
  isValidPreviewPose,
  isValidTeleopDraft,
  isValidToolDraft,
  parseTeleopDraftFields,
  parseToolPoseFields,
  TELEOP_DRAFT_KEYS,
  teleopDraftFields,
  toolPoseFields,
  type TeleopDraft,
} from "../tool-draft";

const pose = (): PreviewPose => ({
  position: [0.0123, -0.0456, 0.0789],
  quaternion: [0, 0, 0, 1],
});

describe("tool and teleoperation configuration drafts", () => {
  test("missing calibration stays null and missing runtime keys are invalid", () => {
    const draft = cloneToolDraft(DEFAULT_TOOL_DRAFT);
    expect(draft).toEqual({ mount: null, tcp: null });
    expect(toolPoseFields(draft.mount)).toEqual(["", "", "", "", "", ""]);
    expect(parseToolPoseFields(toolPoseFields(draft.mount))).toBeNull();
    expect(isValidToolDraft({})).toBe(false);
    expect(isValidToolDraft({ mount: null })).toBe(false);
    expect(isValidToolDraft({ mount: undefined, tcp: null })).toBe(false);
    expect(() =>
      cloneToolDraft({ mount: undefined, tcp: null } as never),
    ).toThrow();
  });

  test("rejects nonfinite, sparse, malformed and non-unit transforms", () => {
    const bad = [
      null,
      {},
      { position: [0, 0], quaternion: [0, 0, 0, 1] },
      { position: new Array(3), quaternion: [0, 0, 0, 1] },
      { position: [0, 0, Infinity], quaternion: [0, 0, 0, 1] },
      { position: [0, NaN, 0], quaternion: [0, 0, 0, 1] },
      { position: [0, 0, 0], quaternion: [0, 0, 0, 0] },
      { position: [0, 0, 0], quaternion: [0, 0, 0, 2] },
      { position: [0, 0, 0], quaternion: [0, 0, NaN, 1] },
      { position: [0, 0, 0], quaternion: new Array(4) },
      { position: ["0", 0, 0], quaternion: [0, 0, 0, 1] },
    ];
    for (const value of bad) {
      expect(isValidPreviewPose(value)).toBe(false);
      expect(isValidToolDraft({ mount: value, tcp: pose() })).toBe(
        value === null,
      );
      if (value !== null) {
        expect(() =>
          cloneToolDraft({ mount: value, tcp: null } as never),
        ).toThrow();
      }
    }
    expect(isValidPreviewPose(pose())).toBe(true);
  });

  test("clones both transforms and their arrays without rewriting source values", () => {
    const shared = pose();
    const source = { mount: shared, tcp: shared };
    const saved = cloneToolDraft(source);
    expect(saved).toEqual(source);
    expect(saved).not.toBe(source);
    expect(saved.mount).not.toBe(shared);
    expect(saved.tcp).not.toBe(shared);
    expect(saved.mount).not.toBe(saved.tcp);
    saved.mount!.position[0] = 10;
    saved.tcp!.quaternion[0] = 0.2;
    expect(shared).toEqual(pose());
    expect(saved.tcp!.position[0]).toBe(shared.position[0]);
    expect(saved.mount!.quaternion).toEqual(shared.quaternion);
  });

  test("round-trips meters/xyzw through millimeters and XYZ degrees", () => {
    const q = new Quaternion().setFromEuler(new Euler(0.2, -0.3, 0.4, "XYZ"));
    const original: PreviewPose = {
      position: [0.0123, -0.0456, 0.0789],
      quaternion: [q.x, q.y, q.z, q.w],
    };
    const fields = toolPoseFields(original);
    expect(fields.slice(0, 3)).toEqual(["12.3", "-45.6", "78.9"]);
    const parsed = parseToolPoseFields(fields)!;
    expect(parsed).not.toBeNull();
    original.position.forEach((value, index) =>
      expect(Math.abs(parsed.position[index] - value)).toBeLessThan(0.00006),
    );
    expect(q.angleTo(new Quaternion(...parsed.quaternion))).toBeLessThan(0.002);
    expect(
      parseToolPoseFields(["10", "20", "-30", "0", "0", "180"])?.position,
    ).toEqual([0.01, 0.02, -0.03]);
  });

  test("invalid transform fields cannot yield a replacement calibration", () => {
    const previous = { mount: pose(), tcp: null };
    const snapshot = cloneToolDraft(previous);
    for (const value of ["", " ", "NaN", "Infinity", "-Infinity", "2001"]) {
      expect(parseToolPoseFields([value, "0", "0", "0", "0", "0"])).toBeNull();
    }
    expect(parseToolPoseFields(["0", "0", "0", "361", "0", "0"])).toBeNull();
    expect(parseToolPoseFields(new Array(6) as PoseFields)).toBeNull();
    expect(previous).toEqual(snapshot);
  });

  test("teleoperation defaults remain unspecified through fields and clone", () => {
    const fields = teleopDraftFields(EMPTY_TELEOP_DRAFT);
    for (const key of TELEOP_DRAFT_KEYS) expect(fields[key]).toBe("");
    expect(parseTeleopDraftFields(fields)).toEqual(EMPTY_TELEOP_DRAFT);
    expect(isValidTeleopDraft({})).toBe(false);
    expect(
      isValidTeleopDraft({
        ...EMPTY_TELEOP_DRAFT,
        commandTimeoutMs: undefined,
      }),
    ).toBe(false);
    const clone = cloneTeleopDraft(EMPTY_TELEOP_DRAFT);
    clone.linearSpeedMps = 0.01;
    expect(EMPTY_TELEOP_DRAFT.linearSpeedMps).toBeNull();
  });

  test("each teleoperation field rejects nonpositive/nonfinite values without inventing limits", () => {
    for (const key of TELEOP_DRAFT_KEYS) {
      for (const value of [0, -1, NaN, Infinity, -Infinity, "1", undefined]) {
        const bad = { ...EMPTY_TELEOP_DRAFT, [key]: value };
        expect(isValidTeleopDraft(bad)).toBe(false);
        expect(() => cloneTeleopDraft(bad as TeleopDraft)).toThrow();
      }
      for (const value of ["0", "-1", "NaN", "Infinity", "-Infinity"]) {
        const fields = {
          ...teleopDraftFields(EMPTY_TELEOP_DRAFT),
          [key]: value,
        };
        expect(parseTeleopDraftFields(fields)).toBeNull();
      }
    }
    const fields = {
      ...teleopDraftFields(EMPTY_TELEOP_DRAFT),
      linearSpeedMps: "0.000001",
      angularSpeedRadps: " ",
      commandTimeoutMs: "1000000000",
      gripperRatePerSec: "0.25",
    };
    const parsed = parseTeleopDraftFields(fields)!;
    expect(parsed.linearSpeedMps).toBe(0.000001);
    expect(parsed.angularSpeedRadps).toBeNull();
    expect(parsed.commandTimeoutMs).toBe(1000000000);
    expect(parsed.gripperRatePerSec).toBe(0.25);
    expect(cloneTeleopDraft(parsed)).toEqual(parsed);
    expect(parseTeleopDraftFields(teleopDraftFields(parsed))).toEqual(parsed);
  });
});
