import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  GRIPPER_LABELS,
  gripperCounts,
  gripperDeclared,
  gripperLabel,
  gripperTitle,
  sourceCounts,
  stopsExport,
  warningText,
  type PoolWarning,
} from "../types";

const zhT = (key: string) =>
  (zh as Record<string, string>)[key.replace(/\s+/g, " ").trim()] ?? key;
const enT = (key: string) => key;

describe("gripper labels", () => {
  test("known classes get names, others are shown as recorded", () => {
    expect(gripperLabel("robotiq_2f85", enT)).toBe("Robotiq 2F-85");
    expect(gripperLabel("franka_hand", zhT)).toBe("Franka 原装夹爪");
    expect(gripperLabel("unknown", zhT)).toBe("未知夹爪");
    expect(gripperLabel("my_gripper", enT)).toBe("my_gripper");
    expect(gripperLabel(null, enT)).toBe("Unknown gripper");
  });

  test("counts are listed largest first, unknown last on a tie", () => {
    expect(
      gripperCounts({ unknown: 5, franka_hand: 5, robotiq_2f85: 9 }, enT),
    ).toBe("Robotiq 2F-85 9 · Franka Hand 5 · Unknown gripper 5");
  });

  test("every label is in both catalogs", () => {
    for (const label of Object.values(GRIPPER_LABELS)) {
      expect(label in en).toBe(true);
      expect(label in zh).toBe(true);
    }
  });
});

describe("gripper warnings", () => {
  const base = { code: "mixed_gripper", message: "", blocking: true };
  const counts = { robotiq_2f85: 120, franka_hand: 30 };

  test("a mixed selection blocks the export and says what it holds", () => {
    const w: PoolWarning = {
      ...base,
      problem: "mixed_known",
      allowed: false,
      counts,
    };
    expect(stopsExport(w)).toBe(true);
    expect(warningText(w, enT)).toContain(
      "mixes grippers: Robotiq 2F-85 120 · Franka Hand 30",
    );
    const text = warningText(w, zhT);
    expect(text).toContain("Robotiq 2F-85 120 · Franka 原装夹爪 30");
    expect(text).toContain("混用");
  });

  test("a known gripper next to unknown has its own wording", () => {
    const w: PoolWarning = {
      ...base,
      problem: "known_and_unknown",
      counts: { robotiq_2f85: 3, unknown: 2 },
    };
    expect(warningText(w, enT)).toContain("not recorded");
    expect(warningText(w, zhT)).toContain("未记录");
  });

  test("several sources with no gripper record block and name the sources", () => {
    const w: PoolWarning = {
      ...base,
      problem: "unknown_multi_source",
      counts: { unknown: 7 },
      unknown_sources: {
        "data_collection/a": 4,
        "data_collection_robotiq/b": 3,
      },
    };
    expect(stopsExport(w)).toBe(true);
    expect(warningText(w, enT)).toContain(
      "data_collection/a 4 · data_collection_robotiq/b 3",
    );
    expect(warningText(w, zhT)).toContain("多个没有夹爪记录的来源");
    expect(sourceCounts({ a: 1, b: 2, c: 3, d: 4, e: 5, f: 6 })).toContain(
      "+1",
    );
  });

  test("an allowed mix and the unknown note do not block", () => {
    const allowed: PoolWarning = {
      ...base,
      blocking: false,
      allowed: true,
      counts,
    };
    expect(stopsExport(allowed)).toBe(false);
    expect(warningText(allowed, enT)).toContain("on purpose");
    const unknown: PoolWarning = {
      code: "gripper_unknown",
      message: "",
      blocking: false,
      episodes: 4,
      unknown_sources: { legacy: 4 },
    };
    expect(warningText(unknown, enT)).toContain("4 episode(s) from legacy 4");
    expect(warningText(unknown, zhT)).toContain("来自 legacy 4 的 4 个片段");
    const where: PoolWarning = {
      ...unknown,
      unknown_sources: { legacy: 3, legacy2: 1 },
    };
    expect(warningText(where, enT)).toContain("from legacy 3 · legacy2 1");
  });

  test("unknown from several sources blocks and names the sources", () => {
    const w: PoolWarning = {
      code: "mixed_gripper",
      blocking: true,
      problem: "unknown_multi_source",
      message: "",
      counts: { unknown: 4 },
      unknown_sources: { legacy: 2, legacy2: 2 },
    };
    expect(stopsExport(w)).toBe(true);
    expect(warningText(w, enT)).toContain(
      "several sources: legacy 2 · legacy2 2",
    );
    const zhText = warningText(w, zhT);
    expect(zhText).toContain("legacy 2 · legacy2 2");
    expect(zhText).toContain("pool/rules.json");
    expect(zhText).not.toContain("旧数据");
    const many: Record<string, number> = {};
    for (let i = 0; i < 8; i++) many[`s${i}`] = 8 - i;
    expect(sourceCounts(many)).toBe("s0 8 · s1 7 · s2 6 · s3 5 · s4 4 · +3");
  });

  test("every sentence is translated", () => {
    for (const key of [
      "The selection mixes grippers: {counts}. An export takes one gripper: filter by gripper, or allow mixing.",
      "The selection mixes episodes of a known gripper with episodes whose gripper is not recorded: {counts}. Filter by gripper, list Unknown gripper on purpose, or allow mixing.",
      "Grippers are mixed on purpose: {counts}.",
      "{count} episode(s) from {sources} have no recorded gripper (unknown): their metadata does not say which gripper was used.",
      "The selection takes episodes with no recorded gripper from several sources: {sources}. A source with no gripper record may hold either gripper. Declare each source's gripper in pool/rules.json, list Unknown gripper on purpose, or allow mixing.",
      "No gripper is recorded for these sources; their episodes export as unknown.",
      "Robot",
      "Action mode",
      "End-effector frame",
      "declared",
      "{count} episode(s): the gripper comes from a declaration in pool/rules.json, not from the metadata.",
    ]) {
      expect(key in en).toBe(true);
      expect(zhT(key)).not.toBe(key);
    }
  });
});

describe("declared grippers", () => {
  test("a declaration is told apart from a reading and the tooltip is translated", () => {
    expect(
      gripperDeclared({ embodiment_evidence: { gripper: "declared: a (me)" } }),
    ).toBe(true);
    expect(
      gripperDeclared({
        embodiment_evidence: { gripper: "metadata:gripper_joint_names" },
      }),
    ).toBe(false);
    expect(gripperDeclared({})).toBe(false);
    // Taken from a declared linked capture: still a declaration.
    expect(
      gripperDeclared({
        embodiment_evidence: { gripper: "linked capture: declared: a" },
      }),
    ).toBe(true);
    // Agreeing with the metadata, or contradicting it, is not.
    for (const text of [
      "metadata:x; declared agrees (a)",
      "conflict: metadata robotiq_2f85 (m); declared franka_hand (a)",
    ])
      expect(gripperDeclared({ embodiment_evidence: { gripper: text } })).toBe(
        false,
      );
    const row = { robot: "franka_fr3", gripper: "franka_hand" };
    const text = gripperTitle(row, zhT) || "";
    expect(text).toContain("机器人: franka_fr3");
    expect(text).toContain("末端坐标系: unknown");
  });
});
