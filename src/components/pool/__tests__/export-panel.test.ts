import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { outputDirProblem } from "../export-panel";
import { bytes, duration } from "../pool-progress";
import {
  TIMING_HINTS,
  TIMING_LABELS,
  defaultTiming,
  stopsExport,
  warningText,
  type PoolWarning,
} from "../types";

describe("outputDirProblem", () => {
  const roots = ["/data/exports/"];
  test("accepts empty, inside and home paths", () => {
    expect(outputDirProblem("", roots)).toBeNull();
    expect(outputDirProblem("/data/exports", roots)).toBeNull();
    expect(outputDirProblem("/data/exports/pi05/", roots)).toBeNull();
    expect(outputDirProblem("~/x", roots)).toBeNull();
  });
  test("refuses outside and relative folders", () => {
    expect(outputDirProblem("/data/exports2", roots)).toBe("outside");
    expect(outputDirProblem("/etc", roots)).toBe("outside");
    expect(outputDirProblem("relative/dir", roots)).toContain("absolute");
  });
});

describe("progress formatting", () => {
  test("bytes and duration", () => {
    expect(bytes(0)).toBe("0 B");
    expect(bytes(1536)).toBe("1.5 KB");
    expect(duration(65)).toBe("1m 05s");
    expect(duration(null)).toBe("—");
  });
});

describe("timing", () => {
  const zhT = (key: string) =>
    (zh as Record<string, string>)[key.replace(/\s+/g, " ").trim()] ?? key;
  const enT = (key: string) => key;

  test("defaults per format; a raw copy has none", () => {
    expect(defaultTiming("lerobot_v21")).toBe("resample");
    expect(defaultTiming("recap_value")).toBe("retime");
    expect(defaultTiming("raw_capture")).toBeNull();
  });

  test("only blocking or certainly refused warnings stop the export", () => {
    const base = { code: "x", message: "", blocking: false };
    expect(stopsExport(base)).toBe(false);
    expect(stopsExport({ ...base, level: "info" })).toBe(false);
    expect(stopsExport({ ...base, blocking: true })).toBe(true);
    expect(stopsExport({ ...base, refused: true })).toBe(true);
  });

  test("timing warnings carry their numbers in both languages", () => {
    const below: PoolWarning = {
      code: "source_fps_below_export",
      blocking: false,
      refused: true,
      message: "",
      episodes: 12,
      export_fps: 10,
      min_source_fps: 9.36,
      suggested_fps: 9,
    };
    expect(warningText(below, enT)).toContain(
      "12 raw episode(s) were recorded below the export FPS 10 (as slow as 9.36 FPS)",
    );
    expect(warningText(below, enT)).toContain("Lower the FPS to 9");
    expect(warningText(below, zhT)).toBe(
      "12 个原始片段的录制帧率低于导出帧率 10（最低 9.36）。重采样不会补帧，导出会被拒绝。请把导出帧率降到 9，或把时间模式改为重定时。",
    );
    const note: PoolWarning = {
      code: "retime_time_scale",
      blocking: false,
      level: "info",
      message: "",
      episodes: 3,
      max_deviation_percent: 5.3,
      direction: "shorter",
    };
    expect(warningText(note, zhT)).toBe(
      "重定时：3 个原始片段的时间轴最多缩短 5.3%（动作播放相应变快）。",
    );
    expect(warningText({ ...note, direction: "longer" }, zhT)).toContain(
      "最多拉长 5.3%",
    );
  });

  test("the timing catalog entries exist in both languages", () => {
    for (const key of [
      "Timing",
      ...Object.values(TIMING_LABELS),
      ...Object.values(TIMING_HINTS),
    ]) {
      expect((en as Record<string, string>)[key]).toBeTruthy();
      expect((zh as Record<string, string>)[key]).toBeTruthy();
    }
    expect((zh as Record<string, string>)[TIMING_HINTS.retime]).toBe(
      "保留每一帧，按导出帧率声明（时间轴略有伸缩，动作播放略快或略慢）",
    );
    expect((zh as Record<string, string>)[TIMING_HINTS.resample]).toBe(
      "只丢帧，不补帧（源帧率低于导出帧率时会被拒绝）",
    );
  });
});
