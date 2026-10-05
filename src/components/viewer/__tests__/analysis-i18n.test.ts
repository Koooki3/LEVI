import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const components = join(import.meta.dir, "../..");
/** The files of the analysis, viewer-tools and diagnostics views. */
const FILES = [
  "action-insights-panel.tsx",
  "filtering-panel.tsx",
  "stats-panel.tsx",
  "overview-panel.tsx",
  "data-recharts.tsx",
  "urdf-viewer.tsx",
  "levi-doctor.tsx",
  "annotations-timeline.tsx",
  "recap-value-section.tsx",
  "object-annotation-panel.tsx",
  "fast-segmentation-panel.tsx",
  "viewer/analysis-ui.tsx",
  "viewer/popup-actions.tsx",
];

/** Keys written as `t("…")` and the two arms of `t(cond ? "…" : "…")`. */
function keysOf(file: string): string[] {
  const text = readFileSync(join(components, file), "utf8");
  const keys = new Set<string>();
  for (const m of text.matchAll(/\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]/g))
    keys.add(m[1]);
  for (const m of text.matchAll(
    /\bt\(\s*[^()"`]*\?\s*"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"/g,
  ))
    keys.add(m[1]).add(m[2]);
  // Titles of AnalysisCard are translated by the card itself.
  for (const m of text.matchAll(/<AnalysisCard\s+title="([^"]+)"/g))
    keys.add(m[1]);
  return [...keys].map((k) => k.replace(/\s+/g, " ").trim());
}

describe("analysis views are in both catalogs", () => {
  test.each(FILES)(
    "%s: every t() key has an English and a Chinese entry",
    (file) => {
      const missing = keysOf(file).filter(
        (key) => !(key in en) || !(key in zh),
      );
      expect(missing).toEqual([]);
    },
  );

  test("sentence keys keep their {placeholders} in Chinese", () => {
    const bad: string[] = [];
    for (const key of Object.keys(en)) {
      const slots = key.match(/\{[a-z]+\}/g);
      if (!slots) continue;
      const value = (zh as Record<string, string>)[key] ?? "";
      for (const slot of slots) if (!value.includes(slot)) bad.push(key);
    }
    expect(bad).toEqual([]);
  });

  test("sample sizes and delays are whole sentences, not glued fragments", () => {
    const z = zh as Record<string, string>;
    expect(z["({n} episodes sampled)"]).toBe("（已采样 {n} 个片段）");
    expect(z["{n} episodes sampled"]).toBe("已采样 {n} 个片段");
    expect(z["Mean control delay: {n} steps ({s}s)"]).toContain("{n}");
    expect(z["Std Dev"]).toBe("标准差");
  });
});

describe("terms (片段 / 时间片段)", () => {
  const z = zh as Record<string, string>;
  test("the pilot episode is a 试标片段", () => {
    expect(z["Pilot episode"]).toBe("试标片段");
    expect(z["Pilot episode"]).not.toMatch(/Episode/);
  });

  test("splitting and merging act on 时间片段", () => {
    expect(z["Split segment at midpoint"]).toContain("时间片段");
    expect(z["Merge next matching segment"]).toContain("时间片段");
  });
});
