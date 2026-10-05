import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import {
  DATA_ON_MEDIA,
  DATA_SERIES,
  mediaColor,
  seriesColor,
} from "../data-palette";

const dir = join(import.meta.dir, "..");
const viewerCss = readFileSync(join(dir, "viewer.css"), "utf8");
const annotationsCss = readFileSync(join(dir, "annotations.css"), "utf8");
const tokensCss = readFileSync(join(dir, "../../styles/tokens.css"), "utf8");

/** `--ds-data-N: #hex` of one block of tokens.css, in slot order. */
function slots(block: string): string[] {
  return [...block.matchAll(/--ds-data-(\d): (#[0-9a-f]{6});/g)]
    .sort((a, b) => Number(a[1]) - Number(b[1]))
    .map((m) => m[2]);
}

function block(css: string, selector: string): string {
  const start = css.indexOf(selector);
  expect(start).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start));
}

describe("viewer data palette", () => {
  test("eight fixed slots, repeated in order past eight", () => {
    expect(DATA_SERIES).toHaveLength(8);
    expect(seriesColor(0)).toBe("var(--dv-1)");
    expect(seriesColor(8)).toBe("var(--dv-1)");
    expect(seriesColor(-1)).toBe("var(--dv-8)");
    expect(mediaColor(9)).toBe(DATA_ON_MEDIA[1]);
  });

  test("the system and manual dark blocks are identical, and canvas uses the dark steps", () => {
    const system = slots(
      block(
        tokensCss,
        '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {',
      ),
    );
    const manual = slots(
      block(tokensCss, '[data-theme="dark"] {\n  color-scheme: dark;'),
    );
    expect(system).toHaveLength(8);
    expect(manual).toEqual(system);
    expect([...DATA_ON_MEDIA]).toEqual(system);
  });

  test("light and dark steps differ (dark is selected, not flipped)", () => {
    const light = slots(block(tokensCss, ':root,\n[data-theme="light"] {'));
    expect(light).toHaveLength(8);
    expect(light).not.toEqual([...DATA_ON_MEDIA]);
  });

  test("every theme scope of viewer.css points --dv-N at --ds-data-N", () => {
    for (const selector of [
      ':root,\n[data-theme="light"] {',
      ':root:not([data-theme="light"]) {',
      '[data-theme="dark"] {',
    ]) {
      const text = block(viewerCss, selector);
      for (let n = 1; n <= 8; n++)
        expect(text).toContain(`--dv-${n}: var(--ds-data-${n});`);
    }
  });

  test("no colour literal outside the palette blocks", () => {
    const withoutPalette = viewerCss.replace(
      /(:root,\n\[data-theme="light"\]|:root:not\(\[data-theme="light"\]\)|\[data-theme="dark"\]) \{[^}]*\}/g,
      "",
    );
    const literal = /#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/;
    expect(withoutPalette).not.toMatch(literal);
    expect(annotationsCss).not.toMatch(literal);
  });

  test("text in the viewer styles is never smaller than 12 px", () => {
    for (const css of [viewerCss, annotationsCss]) {
      const sizes = [...css.matchAll(/font-size: (\d+(?:\.\d+)?)px/g)].map(
        (m) => Number(m[1]),
      );
      expect(sizes.every((size) => size >= 12)).toBe(true);
    }
  });

  test("no uppercase or widened tracking in the annotation styles", () => {
    expect(annotationsCss).not.toContain("text-transform: uppercase");
  });
});

describe("series past eight", () => {
  test("the colour repeats but the line pattern does not", async () => {
    const { seriesDash } = await import("../data-palette");
    expect(seriesDash(0)).toBeUndefined();
    expect(seriesDash(7)).toBeUndefined();
    expect(seriesDash(8)).toBe("6 3");
    expect(seriesColor(8)).toBe(seriesColor(0));
    expect(seriesDash(16)).not.toBe(seriesDash(8));
  });
});
