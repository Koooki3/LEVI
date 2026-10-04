import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

/**
 * The design tokens, read from tokens.css itself: key colour pairs must reach
 * the WCAG thresholds of the UI redesign proposal (§4.3–4.5): 4.5:1 for text,
 * 3:1 for control boundaries, focus rings and progress.
 */

const STYLES = join(import.meta.dir, "../../../styles");
const TOKENS = readFileSync(join(STYLES, "tokens.css"), "utf8");
const DS = readFileSync(join(STYLES, "ds.css"), "utf8");
const DESIGN_PAGE = readFileSync(
  join(import.meta.dir, "../../../app/design/design.css"),
  "utf8",
);

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

function declarations(body: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const match of body.matchAll(/(--ds-[a-z0-9-]+)\s*:\s*([^;]+);/g))
    out[match[1]] = match[2].trim();
  return out;
}

const RAW = declarations(block(TOKENS, /^:root \{/m));
const LIGHT = declarations(
  block(TOKENS, /^:root,\n\[data-theme="light"\] \{/m),
);
const DARK_MEDIA = declarations(
  block(
    block(TOKENS, /^@media \(prefers-color-scheme: dark\) \{/m),
    /:root:not\(\[data-theme="light"\]\) \{/,
  ),
);
const DARK = declarations(block(TOKENS, /^\[data-theme="dark"\] \{/m));

function resolve(theme: Record<string, string>, name: string): string {
  let value = theme[name] ?? RAW[name];
  for (let i = 0; i < 10 && value?.startsWith("var("); i += 1) {
    const ref = /^var\((--ds-[a-z0-9-]+)\)$/.exec(value)?.[1];
    if (!ref) break;
    value = theme[ref] ?? RAW[ref];
  }
  if (!value || !/^#[0-9a-f]{6}$/i.test(value))
    throw new Error(`${name} does not resolve to a hex colour: ${value}`);
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

const TEXT = 4.5;
const NON_TEXT = 3;

/** [foreground token, background token, minimum] — checked in both themes. */
const PAIRS: Array<[string, string, number]> = [
  ...[
    "--ds-bg",
    "--ds-surface-1",
    "--ds-surface-2",
    "--ds-surface-sunken",
    "--ds-surface-hover",
    "--ds-surface-selected",
  ].flatMap(
    (bg): Array<[string, string, number]> => [
      ["--ds-text-primary", bg, TEXT],
      ["--ds-text-secondary", bg, TEXT],
      ["--ds-focus-ring", bg, NON_TEXT],
    ],
  ),
  ["--ds-text-primary", "--ds-bg-reading", TEXT],
  ["--ds-text-primary", "--ds-field-bg", TEXT],
  ["--ds-text-tertiary", "--ds-bg", TEXT],
  ["--ds-text-tertiary", "--ds-surface-1", TEXT],
  ["--ds-text-tertiary", "--ds-field-bg", TEXT],
  ["--ds-text-tertiary-on-sunken", "--ds-surface-sunken", TEXT],
  ["--ds-text-tertiary-on-hover", "--ds-surface-hover", TEXT],
  ["--ds-text-tertiary-on-selected", "--ds-surface-selected", TEXT],
  ["--ds-text-tertiary-on-raised", "--ds-surface-2", TEXT],
  ["--ds-border-control", "--ds-bg", NON_TEXT],
  ["--ds-border-control", "--ds-surface-1", NON_TEXT],
  ["--ds-border-control", "--ds-surface-sunken", NON_TEXT],
  ["--ds-border-control", "--ds-field-bg", NON_TEXT],
  ["--ds-border-control-on-raised", "--ds-surface-2", NON_TEXT],
  ["--ds-on-accent", "--ds-accent", TEXT],
  ["--ds-on-accent", "--ds-accent-hover", TEXT],
  ["--ds-accent", "--ds-bg", NON_TEXT],
  ["--ds-accent", "--ds-surface-1", NON_TEXT],
  ["--ds-accent", "--ds-surface-2", NON_TEXT],
  ["--ds-selected-indicator", "--ds-surface-selected", NON_TEXT],
  ["--ds-progress", "--ds-progress-track", NON_TEXT],
  ["--ds-on-danger", "--ds-danger", TEXT],
  ...["success", "warning", "danger", "info"].flatMap(
    (tone): Array<[string, string, number]> => [
      [`--ds-${tone}`, "--ds-surface-1", TEXT],
      [`--ds-${tone}`, `--ds-${tone}-bg`, TEXT],
      [`--ds-${tone}`, "--ds-surface-2", TEXT],
      [`--ds-${tone}`, "--ds-field-bg", TEXT],
    ],
  ),
];

describe("token contrast (WCAG 2.x)", () => {
  for (const [name, theme] of [
    ["light", LIGHT],
    ["dark", DARK],
  ] as const) {
    test(`${name}: every key pair reaches its threshold`, () => {
      const failures = PAIRS.flatMap(([fg, bg, min]) => {
        const ratio = contrast(resolve(theme, fg), resolve(theme, bg));
        return ratio + 1e-9 >= min
          ? []
          : [`${fg} on ${bg}: ${ratio.toFixed(2)} < ${min}`];
      });
      expect(failures).toEqual([]);
    });
  }

  test("reproduces the proposal's numbers, including the pairs it rules out", () => {
    const L = (step: number) => RAW[`--ds-gray-l-${step}`];
    const D = (step: number) => RAW[`--ds-gray-d-${step}`];
    const expected: Array<[string, string, number]> = [
      [L(11), L(0), 16.83],
      [L(9), L(0), 7.91],
      [L(8), L(0), 5.07],
      [L(8), L(2), 4.66],
      [L(8), L(3), 4.34], // ruled out: tertiary on sunken
      [L(8), L(4), 3.97], // ruled out: tertiary on hover
      [L(7), L(0), 3.62],
      [L(7), L(2), 3.33],
      [D(12), D(1), 18.07],
      [D(12), D(3), 15.63],
      [D(10), D(5), 6.3],
      [D(9), D(4), 4.75],
      [D(9), D(5), 4.27], // ruled out: tertiary on raised
      [D(8), D(3), 3.36],
      [D(8), D(1), 3.88],
    ];
    for (const [fg, bg, ratio] of expected)
      expect(Math.abs(contrast(fg, bg) - ratio)).toBeLessThan(0.02);
    // The ruled-out pairs really fail, so the swaps above are needed.
    expect(contrast(L(8), L(3))).toBeLessThan(TEXT);
    expect(contrast(D(9), D(5))).toBeLessThan(TEXT);
    expect(contrast(D(8), D(5))).toBeLessThan(NON_TEXT);
  });
});

describe("token structure", () => {
  test("the system-dark and manual-dark blocks are identical", () => {
    expect(DARK_MEDIA).toEqual(DARK);
  });

  test("light and dark define the same semantic tokens", () => {
    expect(Object.keys(DARK).sort()).toEqual(Object.keys(LIGHT).sort());
  });

  test("every custom property is in the --ds- namespace", () => {
    const names = [...TOKENS.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gm)].map(
      (m) => m[1],
    );
    expect(names.length).toBeGreaterThan(100);
    expect(names.filter((name) => !name.startsWith("--ds-"))).toEqual([]);
  });

  test("tokens.css declares no colour-scheme or style on :root itself", () => {
    // Existing pages must look the same: only variables go on :root.
    const rootBodies = [
      block(TOKENS, /^:root \{/m),
      block(TOKENS, /^:root,\n\[data-theme="light"\] \{/m),
    ];
    for (const body of rootBodies) {
      const plain = body
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => /^[a-z][a-z-]*\s*:/.test(line));
      expect(plain).toEqual([]);
    }
  });

  test("reduced motion zeroes the long durations and all movement", () => {
    const media = declarations(
      block(TOKENS, /@media \(prefers-reduced-motion: reduce\) \{/),
    );
    const attribute = declarations(
      block(TOKENS, /^\[data-motion="reduce"\] \{/m),
    );
    expect(attribute).toEqual(media);
    expect(media["--ds-dur-base"]).toBe("0ms");
    expect(media["--ds-dur-slow"]).toBe("0ms");
    expect(media["--ds-ease-spring"]).toBe("linear");
    expect(media["--ds-motion-shift"]).toBe("0px");
    expect(media["--ds-motion-dialog-scale"]).toBe("1");
    expect(media["--ds-motion-press-scale"]).toBe("1");
    // Spinners, shuttles and breathing stop too.
    expect(DS).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{\s*\.ds-spin,\s*\.ds-breathe,[^}]*animation: none;/,
    );
  });

  test("component and page styles use tokens, never colour literals", () => {
    for (const css of [DS, DESIGN_PAGE]) {
      const code = css.replace(/\/\*[\s\S]*?\*\//g, "");
      const literals = code.match(
        /#[0-9a-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(/gi,
      );
      expect(literals).toBeNull();
    }
  });

  test("every token the styles use is defined", () => {
    const defined = new Set(
      [...TOKENS.matchAll(/(--ds-[a-z0-9-]+)\s*:/g)].map((m) => m[1]),
    );
    const used = new Set(
      [...(DS + DESIGN_PAGE).matchAll(/var\((--ds-[a-z0-9-]+)/g)].map(
        (m) => m[1],
      ),
    );
    expect([...used].filter((name) => !defined.has(name))).toEqual([]);
  });
});
