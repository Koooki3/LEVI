import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

// The view model and the components name catalogue keys as plain strings
// (a key is looked up by `t(card.title)`, not written in a `t("…")` call), so
// the catalogue test does not see them. This one reads the sources.
const src = join(import.meta.dir, "../../..");
const SOURCES = [
  "components/rig/rig-logic.ts",
  "components/rig/rig-card.tsx",
  "components/rig/rig-say.ts",
  "components/rig/use-rig-poll.ts",
  "app/rig/page.tsx",
  // The action cards.
  "components/rig/action-parts.tsx",
  "components/rig/catalog-logic.ts",
  "components/rig/command-logic.ts",
  "components/rig/command-panel.tsx",
  "components/rig/deploy-logic.ts",
  "components/rig/deploy-parts.tsx",
  "components/rig/deploy-panel.tsx",
  "components/rig/rig-actions.tsx",
  "components/rig/rig-failure.ts",
];
const named = new Set<string>();
for (const file of SOURCES)
  for (const match of readFileSync(join(src, file), "utf8").matchAll(
    /"(rig\.[a-z0-9_.]+)"/g,
  ))
    named.add(match[1]);

const both = (key: string) => key in en && key in zh;

describe("the Rig console's catalogue entries", () => {
  test("every key the code names is in both languages", () => {
    expect(named.size).toBeGreaterThan(100);
    expect([...named].filter((key) => !both(key))).toEqual([]);
  });

  test("no rig.* entry is left over", () => {
    const entries = Object.keys(en).filter((key) => key.startsWith("rig."));
    expect(entries.filter((key) => !named.has(key))).toEqual([]);
    expect(Object.keys(zh).filter((key) => key.startsWith("rig."))).toEqual(
      entries,
    );
  });

  test("the page, its navigation entry and its chord have both languages", () => {
    for (const key of ["Rig console", "Go to: Rig console"])
      expect(both(key)).toBe(true);
    expect((zh as Record<string, string>)["Rig console"]).toBe("机器人台");
  });

  test("the scope texts say the backend runs read-only queries, in both languages", () => {
    for (const catalogue of [en, zh] as Array<Record<string, string>>)
      for (const key of ["rig.scope.read_only", "rig.gpu.hint"]) {
        expect(catalogue[key]).toContain("nvidia-smi");
        expect(catalogue[key]).toContain("systemctl --user list-units");
      }
  });

  test("no literal 'unknown' stands in for a missing value: the catalogue says it", () => {
    for (const file of SOURCES) {
      const code = readFileSync(join(src, file), "utf8");
      expect(code.match(/(\?\?|\|\|)\s*"unknown"/g) ?? []).toEqual([]);
    }
  });

  test("Chinese text keeps the placeholders of the English text", () => {
    for (const key of named) {
      const placeholders = (text: string) =>
        [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
      expect(placeholders((zh as Record<string, string>)[key])).toEqual(
        placeholders((en as Record<string, string>)[key]),
      );
    }
  });
});
