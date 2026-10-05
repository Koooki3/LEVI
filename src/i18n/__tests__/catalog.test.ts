import { describe, expect, test } from "bun:test";
import ts from "typescript";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";

const src = join(import.meta.dir, "../..");
const enKeys = Object.keys(en);
const zhKeys = Object.keys(zh);

type Found = { key: string; where: string };
type Template = { prefix: string; where: string };

/** The strings an expression can evaluate to when they are written out:
 * string literals, both arms of `a ? "x" : "y"`, `a ?? "x"`, `a || "x"`, a
 * parenthesis, and a template without substitutions; a template with
 * substitutions is returned as its static prefix. */
function stringsOf(
  node: ts.Expression,
  out: { literals: string[]; templates: string[] },
): void {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node))
    out.literals.push(node.text);
  else if (ts.isTemplateExpression(node)) out.templates.push(node.head.text);
  else if (ts.isParenthesizedExpression(node)) stringsOf(node.expression, out);
  else if (ts.isConditionalExpression(node)) {
    stringsOf(node.whenTrue, out);
    stringsOf(node.whenFalse, out);
  } else if (
    ts.isBinaryExpression(node) &&
    [ts.SyntaxKind.QuestionQuestionToken, ts.SyntaxKind.BarBarToken].includes(
      node.operatorToken.kind,
    )
  ) {
    stringsOf(node.left, out);
    stringsOf(node.right, out);
  }
}

const fold = (text: string) => text.replace(/\s+/g, " ").trim();

/** What the code asks the catalogue for: every `t(…)` argument (literals,
 * both arms of a conditional, template prefixes) and every expression inside a
 * `<T>` ({cond ? "a" : "b"}). Files under __tests__ are skipped. */
function catalogueUses(): { literals: Found[]; templates: Template[] } {
  const literals: Found[] = [];
  const templates: Template[] = [];
  for (const file of new Bun.Glob("**/*.{ts,tsx}").scanSync(src)) {
    if (file.includes("__tests__")) continue;
    const sf = ts.createSourceFile(
      file,
      readFileSync(join(src, file), "utf8"),
      ts.ScriptTarget.Latest,
      true,
      file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );
    const take = (
      expression: ts.Expression,
      node: ts.Node,
      withTemplates: boolean,
    ) => {
      const found = { literals: [] as string[], templates: [] as string[] };
      stringsOf(expression, found);
      const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
      const where = `${file}:${line + 1}`;
      for (const text of found.literals)
        literals.push({ key: fold(text), where });
      // A template inside <T> is composite text the locale's patterns handle
      // ("Speed 2: 3 ep"); only a t(`…`) key is checked.
      if (withTemplates)
        for (const prefix of found.templates) templates.push({ prefix, where });
    };
    const visit = (node: ts.Node, inT: boolean) => {
      let within = inT;
      if (
        ts.isJsxElement(node) &&
        node.openingElement.tagName.getText() === "T"
      )
        within = true;
      if (
        ts.isCallExpression(node) &&
        ts.isIdentifier(node.expression) &&
        node.expression.text === "t" &&
        node.arguments.length >= 1
      )
        take(node.arguments[0], node, true);
      // A child of an element inside <T> ({cond ? "a" : "b"}), not an
      // attribute's value (className, tone): only children are translated.
      if (
        within &&
        ts.isJsxExpression(node) &&
        node.expression &&
        (ts.isJsxElement(node.parent) || ts.isJsxFragment(node.parent))
      )
        take(node.expression, node, false);
      ts.forEachChild(node, (child) => visit(child, within));
    };
    visit(sf, false);
  }
  return { literals, templates };
}

/** The keys a template can produce, where the code builds them from a fixed
 * set. Every dotted template prefix must be listed here, with all its members
 * in the catalogue (a member missing shows its raw key to the reader). */
const TEMPLATE_KEYS: Record<string, string[]> = {
  "report.state.": ["running", "done", "blocked", "waiting", "planned"],
  "report.milestone.": ["done", "current", "next"],
  // The kinds of a workspace change (`SyncChange["kind"]`, workbench/page.tsx).
  "syncNow.": [
    "added",
    "removed",
    "updated",
    "rebuilding",
    "failed",
    "skipped",
  ],
};
/** Templates the locale's own patterns turn into the other language
 * (`Episode 3` → `片段 3`, levi-locale.tsx). */
const PATTERN_TEMPLATES = [
  "Episode ",
  "Positive advantage: ",
  "All tasks (",
  "State changes lag behind actions by ~",
  "Actions lag behind state changes by ~",
];

describe("the language catalogues", () => {
  test("English and Chinese have exactly the same keys", () => {
    expect(enKeys.filter((key) => !(key in zh))).toEqual([]);
    expect(zhKeys.filter((key) => !(key in en))).toEqual([]);
  });

  test("no key is empty and no Chinese text is just the English", () => {
    for (const key of zhKeys)
      expect((zh as Record<string, string>)[key]).toBeTruthy();
  });

  test("every t() argument and <T> expression with written-out strings finds its key", () => {
    const missing = catalogueUses().literals.filter(
      ({ key }) => key && !(key in en && key in zh),
    );
    // A <T> expression can hold code that is not text (a number, a name): only
    // strings with a letter and a space or capital are meant for people.
    const meant = missing.filter(
      ({ key }) => /[A-Za-z]{3}/.test(key) && !SAME_IN_BOTH.includes(key),
    );
    expect(meant.map(({ key, where }) => `${where} ${key}`)).toEqual([]);
  });

  test("keys built from a template exist for every member (and for each prefix)", () => {
    const { templates } = catalogueUses();
    const unknown = templates.filter(
      ({ prefix }) =>
        /^[A-Za-z]+(\.[A-Za-z]+)*\.$/.test(prefix) &&
        !(prefix in TEMPLATE_KEYS),
    );
    expect(unknown.map(({ prefix, where }) => `${where} ${prefix}`)).toEqual(
      [],
    );
    const notPatterns = templates.filter(
      ({ prefix }) =>
        !/^[A-Za-z]+(\.[A-Za-z]+)*\.$/.test(prefix) &&
        !PATTERN_TEMPLATES.includes(prefix),
    );
    // Sentences with numbers in them go through the locale's patterns; a new
    // one needs a pattern (levi-locale.tsx) and an entry in PATTERN_TEMPLATES.
    expect(
      notPatterns.map(({ prefix, where }) => `${where} ${prefix}`),
    ).toEqual([]);
    const missing: string[] = [];
    for (const [prefix, members] of Object.entries(TEMPLATE_KEYS))
      for (const member of members)
        if (!(`${prefix}${member}` in en && `${prefix}${member}` in zh))
          missing.push(`${prefix}${member}`);
    expect(missing).toEqual([]);
    for (const prefix of Object.keys(TEMPLATE_KEYS))
      expect(templates.some((entry) => entry.prefix === prefix)).toBe(true);
  });

  test('example placeholders held in data (placeholder: "…") are in the catalogue', () => {
    // The quick-add forms keep their placeholders in a table and pass each
    // through t(): a missing key would leave an English example in Chinese.
    const missing: string[] = [];
    for (const file of new Bun.Glob("**/*.{ts,tsx}").scanSync(src)) {
      if (file.includes("__tests__")) continue;
      const text = readFileSync(join(src, file), "utf8");
      for (const match of text.matchAll(/^\s+placeholder:\s*"([^"]+)"/gm)) {
        const key = fold(match[1]);
        if (!(key in en && key in zh) && !SAME_IN_BOTH.includes(key))
          missing.push(`${file} ${key}`);
      }
    }
    expect(missing).toEqual([]);
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
        if (within && ts.isJsxText(node))
          key = decodeEntities(foldJsxText(node.text));
        if (
          within &&
          ts.isJsxAttribute(node) &&
          TRANSLATED_ATTRIBUTES.has(node.name.getText()) &&
          node.initializer &&
          ts.isStringLiteral(node.initializer)
        )
          key = node.initializer.text.replace(/\s+/g, " ").trim();
        if (
          key &&
          /[A-Za-z]/.test(key) &&
          key.length > 3 &&
          !(key in en && key in zh)
        ) {
          const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
          missing.push(`${file}:${line + 1} ${JSON.stringify(key)}`);
        }
        ts.forEachChild(node, (child) => visit(child, within));
      };
      visit(sf, false);
    }
    // Names, units, paths and example values read the same in both languages
    // and stay as they are: no entry needed.
    const unnamed = missing.filter(
      (entry) =>
        !SAME_IN_BOTH.some((word) =>
          entry.endsWith(` ${JSON.stringify(word)}`),
        ),
    );
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

/** Product names, paths and example values that read the same in both languages. */
const SAME_IN_BOTH = [
  "Ollama",
  "1038lab/sam3",
  "sam3.pt",
  "workspace/checkpoints/sam3/sam3.pt",
  "Codex Pilot",
  "Claude Code Pilot",
  "Hugging Face",
  "https://provider.example/v1",
  "local/dataset or org/dataset",
  "cup, plate",
  "integrations/segmentation/setup.sh",
  "ms ·",
  "plates-student-v1",
];
