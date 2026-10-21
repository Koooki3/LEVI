/**
 * Layout audit 2026-10-21, P4 (user: "only down, never up"): spacing in CSS
 * comes from the --ds-space-* scale, and breakpoints from BREAKPOINT. A literal
 * px in gap / margin / padding / inset (0, 1px and 2px hairlines aside) and a
 * media width outside BREAKPOINT (or one below it) are counted per file. A
 * file may not go above its allowance; when it goes below, lower the
 * allowance here in the same commit, so the count only ever falls. A file not
 * listed (every new file) is allowed none.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { BREAKPOINT } from "../../../lib/design/motion";

const SRC = join(import.meta.dir, "../../..");

// measured on feat/layout-p4-spacing-check (2026-10-21, after P1's 999 px);
// only lower these
const SPACING_ALLOWED: Record<string, number> = {
  "app/report/report.css": 1,
  "components/pages-ui/pages.css": 221,
  "components/viewer/annotations.css": 233,
  "components/viewer/viewer.css": 2,
  "styles/ds.css": 1,
};
const BREAKPOINTS_ALLOWED: Record<string, number> = {
  "app/report/report.css": 1,
  "components/live/results.css": 1,
  "components/marvel0/marvel0-dock.css": 1,
  "components/marvel0/marvel0-styles.css": 4,
  "components/pages-ui/agent-content.css": 1,
  "components/pages-ui/pages.css": 5,
  "components/viewer/annotations.css": 4,
  "components/viewer/viewer.css": 1,
  "styles/shell.css": 2,
};

const SPACING =
  /(?:^|[;{\s])((?:row-|column-)?gap|margin(?:-[a-z]+)?|padding(?:-[a-z]+)?|inset(?:-[a-z]+)?)\s*:\s*([^;{}]+)/g;
const PX = /(?<![\w.-])(\d+(?:\.\d+)?)px\b/g;
const MEDIA = /@media[^{]*?\((?:max|min)-width:\s*(\d+)px\)/g;
// BREAKPOINT, plus the header's two-row width (shell.css, 999/1000), where
// .ds-page also narrows its sides (layout P1: one left edge for both)
const HEADER_TWO_ROW = 1000;
const ALLOWED_WIDTHS = new Set(
  [...Object.values(BREAKPOINT), HEADER_TWO_ROW].flatMap((w) => [w, w - 1]),
);

function cssFiles(): string[] {
  return [...new Bun.Glob("**/*.css").scanSync({ cwd: SRC })].sort();
}

export function counts(css: string): { spacing: number; breakpoints: number } {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, "");
  let spacing = 0;
  for (const m of text.matchAll(SPACING))
    for (const px of m[2].matchAll(PX))
      if (!["0", "1", "2"].includes(px[1])) spacing += 1;
  let breakpoints = 0;
  for (const m of text.matchAll(MEDIA))
    if (!ALLOWED_WIDTHS.has(Number(m[1]))) breakpoints += 1;
  return { spacing, breakpoints };
}

describe("spacing and breakpoints only go down", () => {
  test("the counter counts what it should", () => {
    const sample = `
      .a { gap: 14px; padding: 0 var(--ds-space-2) 6px; border: 1px solid; }
      .b { margin-top: 2px; inset-inline: 3px; row-gap: 1px; }
      /* .c { gap: 99px } */
      @media (max-width: 899px) { .d { gap: var(--ds-space-2) } }
      @media (max-width: 960px) { .e { padding: 0 } }`;
    expect(counts(sample)).toEqual({ spacing: 3, breakpoints: 1 });
  });

  for (const file of cssFiles()) {
    test(file, () => {
      const got = counts(readFileSync(join(SRC, file), "utf8"));
      const allowedSpacing = SPACING_ALLOWED[file] ?? 0;
      const allowedBreakpoints = BREAKPOINTS_ALLOWED[file] ?? 0;
      expect(
        got.spacing,
        `${file}: ${got.spacing} literal px in spacing, allowance ${allowedSpacing}; use --ds-space-*, or lower the allowance when the count fell`,
      ).toBe(allowedSpacing);
      expect(
        got.breakpoints,
        `${file}: ${got.breakpoints} media widths outside BREAKPOINT (${[...ALLOWED_WIDTHS].join("/")}), allowance ${allowedBreakpoints}`,
      ).toBe(allowedBreakpoints);
    });
  }
});
