// Muted text on the tinted surfaces of the run pages must reach 4.5:1: the
// plain tertiary step does not on the sunken, warning, danger and info tints.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

const TOKENS = readFileSync(
  join(import.meta.dir, "../../../styles/tokens.css"),
  "utf8",
);
const CSS = readFileSync(join(import.meta.dir, "../run-styles.css"), "utf8");

function block(css: string, opener: RegExp): string {
  const match = opener.exec(css);
  if (!match) throw new Error(`no block ${opener}`);
  let depth = 0;
  const start = css.indexOf("{", match.index);
  for (let i = start; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(start + 1, i);
    }
  }
  throw new Error(`unclosed block ${opener}`);
}
const declarations = (body: string) =>
  Object.fromEntries(
    [...body.matchAll(/(--ds-[a-z0-9-]+)\s*:\s*([^;]+);/g)].map((m) => [
      m[1],
      m[2].trim(),
    ]),
  ) as Record<string, string>;

const RAW = declarations(block(TOKENS, /^:root \{/m));
const LIGHT = declarations(
  block(TOKENS, /^:root,\n\[data-theme="light"\] \{/m),
);
const DARK = declarations(block(TOKENS, /^\[data-theme="dark"\] \{/m));

function resolve(theme: Record<string, string>, name: string): string {
  let value = theme[name] ?? RAW[name];
  for (let i = 0; i < 10 && value?.startsWith("var("); i += 1) {
    const ref = /^var\((--ds-[a-z0-9-]+)\)$/.exec(value)?.[1];
    if (!ref) break;
    value = theme[ref] ?? RAW[ref];
  }
  return value;
}
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const TINTS = [
  "--ds-surface-sunken",
  "--ds-warning-bg",
  "--ds-danger-bg",
  "--ds-info-bg",
];

describe("muted text on the tinted surfaces of the run pages", () => {
  test("a note gives its muted text the secondary step", () => {
    const note = block(CSS, /^\.ar-note \{/m);
    expect(declarations(note)["--ds-text-tertiary"]).toBe(
      "var(--ds-text-secondary)",
    );
  });
  test("the secondary step reaches 4.5:1 on every tint, in both themes", () => {
    for (const [name, theme] of [
      ["light", LIGHT],
      ["dark", DARK],
    ] as const)
      for (const tint of TINTS)
        expect(
          contrast(resolve(theme, "--ds-text-secondary"), resolve(theme, tint)),
        ).toBeGreaterThanOrEqual(4.5);
  });
  test("a disabled state node and a current node note use the on-surface steps", () => {
    expect(block(CSS, /^\.ar-node--disabled \{/m)).toContain(
      "var(--ds-text-tertiary-on-sunken)",
    );
    expect(block(CSS, /^\.ar-node--disabled \.ar-node__note \{/m)).toContain(
      "var(--ds-text-tertiary-on-sunken)",
    );
    expect(block(CSS, /^\.ar-node--current \.ar-node__note \{/m)).toContain(
      "var(--ds-text-tertiary-on-selected)",
    );
  });
  test("those on-surface steps reach 4.5:1 on their surfaces", () => {
    for (const theme of [LIGHT, DARK]) {
      expect(
        contrast(
          resolve(theme, "--ds-text-tertiary-on-sunken"),
          resolve(theme, "--ds-surface-sunken"),
        ),
      ).toBeGreaterThanOrEqual(4.5);
      expect(
        contrast(
          resolve(theme, "--ds-text-tertiary-on-selected"),
          resolve(theme, "--ds-surface-selected"),
        ),
      ).toBeGreaterThanOrEqual(4.5);
    }
  });
});
