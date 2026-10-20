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
  "components/marvel0/marvel0-logic.ts",
  "components/marvel0/marvel0-card.tsx",
  "components/marvel0/marvel0-say.ts",
  "components/marvel0/use-marvel0-poll.ts",
  "app/marvel0/page.tsx",
  // The action cards.
  "components/marvel0/action-parts.tsx",
  "components/marvel0/catalog-logic.ts",
  "components/marvel0/command-logic.ts",
  "components/marvel0/command-panel.tsx",
  "components/marvel0/deploy-logic.ts",
  "components/marvel0/deploy-parts.tsx",
  "components/marvel0/deploy-panel.tsx",
  "components/marvel0/marvel0-actions.tsx",
  "components/marvel0/marvel0-failure.ts",
];
const named = new Set<string>();
for (const file of SOURCES)
  for (const match of readFileSync(join(src, file), "utf8").matchAll(
    /"(marvel0\.[a-z0-9_.]+)"/g,
  ))
    named.add(match[1]);

const both = (key: string) => key in en && key in zh;

describe("the marvel0's catalogue entries", () => {
  test("every key the code names is in both languages", () => {
    expect(named.size).toBeGreaterThan(100);
    expect([...named].filter((key) => !both(key))).toEqual([]);
  });

  test("no marvel0.* entry is left over", () => {
    const entries = Object.keys(en).filter((key) => key.startsWith("marvel0."));
    expect(entries.filter((key) => !named.has(key))).toEqual([]);
    expect(Object.keys(zh).filter((key) => key.startsWith("marvel0."))).toEqual(
      entries,
    );
  });

  test("the page, its navigation entry and its chord have both languages", () => {
    for (const key of ["marvel0", "Go to: marvel0"])
      expect(both(key)).toBe(true);
    expect((zh as Record<string, string>)["marvel0"]).toBe("marvel0");
  });

  test("the scope texts say the backend runs read-only queries, in both languages", () => {
    for (const catalogue of [en, zh] as Array<Record<string, string>>)
      for (const key of ["marvel0.scope.read_only", "marvel0.gpu.hint"]) {
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
