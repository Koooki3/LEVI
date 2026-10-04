import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
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

  test("the older page styles carry no colour of the old palette", () => {
    // levi.css colours point at tokens; black stays for media and shadows.
    const hits = code("app/levi.css")
      .split("\n")
      .filter((line) => LITERAL.test(line))
      .filter((line) => !/rgba?\(\s*0[ ,]+0[ ,]+0\b|#000(000)?\b/.test(line));
    expect(hits).toEqual([]);
  });

  test("the old lime, parchment and cyan are gone from the global layer", () => {
    for (const file of [...TOKEN_ONLY, "app/levi.css"])
      expect(code(file)).not.toMatch(/d4f779|f1efdf|38bdf8|101510/i);
  });
});
