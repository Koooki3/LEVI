import { describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "fs";
import { join } from "path";

const src = join(import.meta.dir, "../../..");
/** Stylesheets of the global layer and the pages it owns: tokens only. */
const TOKEN_ONLY = [
  "app/globals.css",
  "styles/home.css",
  "styles/reading.css",
  "app/report/report.css",
];
const LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/;

function code(file: string): string {
  return readFileSync(join(src, file), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
}

describe("global styles", () => {
  test.each(TOKEN_ONLY)("%s has no colour literal", (file) => {
    const hits = code(file)
      .split("\n")
      .filter((line) => LITERAL.test(line));
    expect(hits).toEqual([]);
  });

  test("the old page stylesheet is gone and nothing imports it", () => {
    expect(existsSync(join(src, "app/levi.css"))).toBe(false);
    for (const file of new Bun.Glob("**/*.{ts,tsx,css}").scanSync(src)) {
      if (file.includes("__tests__")) continue;
      expect(readFileSync(join(src, file), "utf8")).not.toMatch(
        /import\s+["'][^"']*levi\.css["']|@import\s+["'][^"']*levi\.css["']/,
      );
    }
  });

  test("the old lime, parchment and cyan are gone from the global layer", () => {
    for (const file of TOKEN_ONLY)
      expect(code(file)).not.toMatch(/d4f779|f1efdf|38bdf8|101510/i);
  });

  test("the old variable names and Tailwind colour remaps are gone", () => {
    const css = code("app/globals.css");
    // `--bg`, `--accent`, `--text-muted`... and `--color-white`,
    // `--color-slate-*`... pointed older pages at the tokens; a Tailwind
    // colour class now means Tailwind's own colour (white stays white).
    expect(css).not.toMatch(
      /--(bg|surface-[012]|border-subtle|border-strong|text-primary|text-muted|text-faint|accent|accent-soft|accent-ring|radius)\s*:/,
    );
    expect(css).not.toMatch(
      /--color-(white|black|cyan|slate|zinc|lime|red|orange|amber|yellow|green|emerald|blue)/,
    );
  });

  test("no Tailwind palette colour class is left in the interface", () => {
    // Colours come from --ds-* tokens: text-(--ds-text-primary), ds-* classes.
    const palette =
      /(^|[\s"'`:!])(text|bg|border|ring|fill|stroke|from|to|via|divide|outline|decoration|accent|caret|placeholder)-(white|cyan|slate|zinc|lime|red|orange|amber|yellow|green|emerald|blue|gray|neutral|stone|sky|teal|indigo|violet|purple|pink|rose|fuchsia)(-\d+)?(\/\d+)?(?![\w-])/;
    const hits: string[] = [];
    for (const file of new Bun.Glob("**/*.{ts,tsx,css}").scanSync(src)) {
      if (file.includes("__tests__")) continue;
      readFileSync(join(src, file), "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .split("\n")
        .forEach((line, index) => {
          if (palette.test(line)) hits.push(`${file}:${index + 1}`);
        });
    }
    expect(hits).toEqual([]);
  });

  test("no coloured fill with white text in one class list", () => {
    // `white` is the primary text colour now: on an accent or status fill it
    // would be the same colour as the fill. Use --ds-on-accent,
    // --ds-on-danger or text-on-media instead.
    const fill =
      /\bbg-(cyan|lime|red|blue|green|emerald|amber|orange|yellow)-\d+\b/;
    const hits: string[] = [];
    for (const file of new Bun.Glob("**/*.tsx").scanSync(src)) {
      if (file.includes("__tests__")) continue;
      const text = readFileSync(join(src, file), "utf8");
      // Each string literal or template segment is one class list.
      for (const match of text.matchAll(/"[^"\n]*"|'[^'\n]*'|`[^`]*`/g))
        for (const part of match[0].split("${"))
          if (fill.test(part) && /(^|[\s"'`:])text-white\b/.test(part))
            hits.push(`${file}: ${part.trim().slice(0, 80)}`);
    }
    expect(hits).toEqual([]);
  });

  test("the report has no looping animation", () => {
    expect(code("app/report/report.css")).not.toMatch(/\binfinite\b/);
  });
});
