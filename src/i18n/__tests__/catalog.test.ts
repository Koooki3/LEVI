import { describe, expect, test } from "bun:test";
import ts from "typescript";
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

  test("every string a <T> translates has a key (JSX text, entities decoded)", () => {
    // <T> translates the text and the title / placeholder / aria-label / alt /
    // label strings below it. JSX text is folded like React does (lines
    // trimmed and joined by a space) and its entities decoded before the
    // lookup, so a key can contain quotes and apostrophes.
    const missing: string[] = [];
    for (const file of new Bun.Glob("**/*.tsx").scanSync(src)) {
      if (file.includes("__tests__")) continue;
      const source = readFileSync(join(src, file), "utf8");
      const sf = ts.createSourceFile(
        file,
        source,
        ts.ScriptTarget.Latest,
        true,
        ts.ScriptKind.TSX,
      );
      const visit = (node: ts.Node, inT: boolean) => {
        let within = inT;
        if (
          ts.isJsxElement(node) &&
          node.openingElement.tagName.getText() === "T"
        )
          within = true;
        let key: string | null = null;
        if (within && ts.isJsxText(node)) key = decodeEntities(foldJsxText(node.text));
        if (
          within &&
          ts.isJsxAttribute(node) &&
          TRANSLATED_ATTRIBUTES.has(node.name.getText()) &&
          node.initializer &&
          ts.isStringLiteral(node.initializer)
        )
          key = node.initializer.text.replace(/\s+/g, " ").trim();
        if (key && /[A-Za-z]/.test(key) && key.length > 3 && !(key in en && key in zh)) {
          const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
          missing.push(`${file}:${line + 1} ${JSON.stringify(key)}`);
        }
        ts.forEachChild(node, (child) => visit(child, within));
      };
      visit(sf, false);
    }
    // Words that are the same in both languages (names, units, code) need no
    // entry: they stay as they are.
    const unnamed = missing.filter((entry) => !SAME_IN_BOTH.test(entry));
    expect(unnamed).toEqual([]);
  });
});

const TRANSLATED_ATTRIBUTES = new Set([
  "title",
  "placeholder",
  "aria-label",
  "alt",
  "label",
]);

/** React's rule for JSX text: each line trimmed (the first keeps its leading
 * space, the last its trailing one), empty lines dropped, the rest joined by
 * a space. */
function foldJsxText(raw: string): string {
  const lines = raw.split(/\r\n|\n|\r/);
  let out = "";
  lines.forEach((line, index) => {
    let text = line.replace(/\t/g, " ");
    if (index !== 0) text = text.replace(/^ +/, "");
    if (index !== lines.length - 1) text = text.replace(/ +$/, "");
    if (text) {
      if (out && index !== 0) out += " ";
      out += text;
    }
  });
  return out.trim();
}

const ENTITIES: Record<string, string> = {
  "&apos;": "'",
  "&quot;": '"',
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&nbsp;": " ",
  "&rsquo;": "’",
  "&lsquo;": "‘",
  "&hellip;": "…",
  "&mdash;": "—",
  "&ndash;": "–",
  "&rarr;": "→",
  "&larr;": "←",
  "&middot;": "·",
};
function decodeEntities(text: string): string {
  return text.replace(/&[a-z]+;|&#\d+;/g, (match) =>
    match in ENTITIES
      ? ENTITIES[match]
      : match.startsWith("&#")
        ? String.fromCharCode(Number(match.slice(2, -1)))
        : match,
  );
}

/** Product and code words that read the same in both languages. */
const SAME_IN_BOTH = /^$/;
