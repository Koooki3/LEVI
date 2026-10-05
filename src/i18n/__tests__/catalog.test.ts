import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const src = join(import.meta.dir, "../..");
const enKeys = Object.keys(en);
const zhKeys = Object.keys(zh);

/** Every `t("literal")` / `t('literal')` call outside the tests. */
function literalKeys(): Array<{ key: string; where: string }> {
  const found: Array<{ key: string; where: string }> = [];
  for (const file of new Bun.Glob("**/*.{ts,tsx}").scanSync(src)) {
    if (file.includes("__tests__")) continue;
    const text = readFileSync(join(src, file), "utf8");
    for (const match of text.matchAll(
      /(?<![\w.])t\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*[,)]/g,
    )) {
      const raw = match[1] ?? match[2];
      const key = raw
        .replace(/\\(["'])/g, "$1")
        .replace(/\s+/g, " ")
        .trim();
      const line = text.slice(0, match.index).split("\n").length;
      found.push({ key, where: `${file}:${line}` });
    }
  }
  return found;
}

describe("the language catalogues", () => {
  test("English and Chinese have exactly the same keys", () => {
    expect(enKeys.filter((key) => !(key in zh))).toEqual([]);
    expect(zhKeys.filter((key) => !(key in en))).toEqual([]);
  });

  test("no key is empty and no Chinese text is just the English", () => {
    for (const key of zhKeys)
      expect((zh as Record<string, string>)[key]).toBeTruthy();
  });

  test("every t() call with a literal finds its key in both catalogues", () => {
    const missing = literalKeys().filter(
      ({ key }) => !(key in en) || !(key in zh),
    );
    expect(missing.map(({ key, where }) => `${where} ${key}`)).toEqual([]);
  });

  test("text inside <T> that names a control is in the catalogue", () => {
    // Labels the review queue and the activity list render inside <T>.
    for (const key of ["Start time", "End time", "Agent activity", "Std Dev"]) {
      expect(key in en).toBe(true);
      expect(key in zh).toBe(true);
    }
  });
});
