import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

const LITERAL = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(/;

function code(file: string): string {
  return readFileSync(join(import.meta.dir, "..", file), "utf8").replace(
    /\/\*[\s\S]*?\*\//g,
    "",
  );
}

describe("viewer.css", () => {
  test("has no colour literal: the data palette comes from --ds-data-N", () => {
    const hits = code("viewer.css")
      .split("\n")
      .filter((line) => LITERAL.test(line))
      // Black stays for media backdrops and shadows.
      .filter((line) => !/rgba?\(\s*0[ ,]+0[ ,]+0\b|#000(000)?\b/.test(line));
    expect(hits).toEqual([]);
  });

  test("the neutral data colour is a token in every theme scope", () => {
    const css = code("viewer.css");
    const neutral = css.match(/--dv-neutral:[^;]+;/g) ?? [];
    expect(neutral.length).toBeGreaterThanOrEqual(3);
    for (const line of neutral) expect(line).toContain("var(--ds-");
  });

  test("every animation stops under [data-motion=reduce] as well", () => {
    const css = code("viewer.css");
    const animated = [...css.matchAll(/(\.[\w-]+)\s*\{[^}]*\banimation:/g)].map(
      (m) => m[1],
    );
    expect(animated.length).toBeGreaterThan(0);
    const missing = animated.filter(
      (selector) => !css.includes(`[data-motion="reduce"] ${selector}`),
    );
    expect(missing).toEqual([]);
  });
});
