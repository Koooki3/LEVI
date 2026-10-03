import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  GRIPPER_LABELS,
  gripperCounts,
  gripperLabel,
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
    };
    expect(warningText(unknown, enT)).toContain("4 episode(s)");
    expect(warningText(unknown, zhT)).toContain("4 个片段");
  });

  test("every sentence is translated", () => {
    for (const key of [
      "The selection mixes grippers: {counts}. An export takes one gripper: filter by gripper, or allow mixing.",
      "The selection mixes episodes of a known gripper with episodes whose gripper is not recorded: {counts}. Filter by gripper, list Unknown gripper on purpose, or allow mixing.",
      "Grippers are mixed on purpose: {counts}.",
      "{count} episode(s) have no recorded gripper (unknown): their metadata does not say which gripper was used.",
    ]) {
      expect(key in en).toBe(true);
      expect(zhT(key)).not.toBe(key);
    }
  });
});
