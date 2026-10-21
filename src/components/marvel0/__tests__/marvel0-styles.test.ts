import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

// The page's stylesheet, read as text: the rules that went wrong once and
// that a render in a test DOM cannot show (no cascade, no layout).

const css = readFileSync(
  join(import.meta.dir, "../marvel0-styles.css"),
  "utf8",
);
/** The stylesheet without its comments. */
const code = css.replace(/\/\*[\s\S]*?\*\//g, "");

describe("the stylesheet of the marvel0", () => {
  test("no colour is written out: every colour is a design token", () => {
    expect(code.match(/#[0-9a-fA-F]{3,8}\b/g)).toBeNull();
    expect(
      code.match(/\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklch)\(/g),
    ).toBeNull();
    // color-mix is for a tint of a token, never of a colour of its own.
    for (const mix of code.matchAll(/color-mix\(([^;]*?)\)\s*[;,]/g))
      expect(mix[1]).toMatch(/var\(--/);
  });

  test("the state colours of a paragraph are scoped to the page: a lone class loses to `.pg-workbench p`", () => {
    // pages.css colours every paragraph of a page (`.pg-workbench p`, one
    // class and one element); `.rg-bad` alone (one class) never took effect.
    for (const cls of ["rg-bad", "rg-ok", "rg-working", "rg-warn"]) {
      expect(code).toContain(`.rg-page .${cls} {`);
      expect(code).not.toMatch(new RegExp(`(^|\\n)\\.${cls} \\{`));
    }
    expect(code).toContain(".rg-page .rg-chain__focus {");
  });

  test("what moves is calm: every animation of the page has its reduced-motion switch", () => {
    const animated = [
      ...code.matchAll(/animation(?:-name)?:\s*([a-z-]+)/g),
    ].map((m) => m[1]);
    expect(animated.length).toBeGreaterThan(0);
    // The wire of a working node and the glyph that breathes: off for a person
    // who asked for less (the system setting) and under the in-app "reduced".
    expect(code).toMatch(
      /prefers-reduced-motion: reduce\)\s*\{[^}]*:root:not\(\[data-motion="full"\]\) \.rg-chain__link--working/,
    );
    expect(code).toMatch(/\[data-motion="reduce"\] \.rg-chain__link--working/);
    expect(code).toMatch(
      /\[data-motion="reduce"\] \.rg-node--working \.rg-node__glyph/,
    );
  });

  test("the chain is seven to a row, four on a tablet and two on a phone, with a wire only inside a row", () => {
    expect(code).toMatch(/\.rg-chain__nodes \{[^}]*--rg-cols: 7/);
    expect(code).toMatch(
      /@media \(max-width: 1099px\) \{\s*\.rg-chain__nodes \{\s*--rg-cols: 4;/,
    );
    expect(code).toMatch(
      /@media \(max-width: 639px\)[\s\S]*\.rg-chain__nodes \{[^}]*--rg-cols: 2/,
    );
    // The last node of a row has no wire to the next one (it would cross to
    // the row below): every fourth on a tablet, every second on a phone.
    expect(code).toMatch(
      /\.rg-chain__item:nth-child\(4n\) \.rg-chain__link \{\s*display: none/,
    );
    expect(code).toMatch(
      /\.rg-chain__item:nth-child\(2n\) \.rg-chain__link \{\s*display: none/,
    );
  });

  test("there is one solid danger button, HOLD: the other stops are outlined in the danger colour", () => {
    expect(code).toMatch(
      /\.ds-btn--secondary\.rg-btn--danger-outline:not\(:disabled\) \{[^}]*border-color: var\(--ds-danger\)[^}]*color: var\(--ds-danger\)/,
    );
    const sources = [
      "stack-controls.tsx",
      "deploy-panel.tsx",
      "run-panel.tsx",
      "run-dock.tsx",
      "stack-panel.tsx",
      "stack-dialog.tsx",
      "run-start.tsx",
    ].map((file) => ({
      file,
      code: readFileSync(join(import.meta.dir, "..", file), "utf8"),
    }));
    // `variant="danger"` is on HOLD (the card's and the dock's), never on a
    // button of the page itself: the confirm buttons of the windows get it
    // from the one confirm window (a stop, End, Exit and go home).
    const solid = sources.flatMap(({ file, code }) =>
      [...code.matchAll(/variant="danger"/g)].map(() => file),
    );
    expect(solid.sort()).toEqual(["run-dock.tsx", "run-panel.tsx"]);
    const window = readFileSync(
      join(import.meta.dir, "../confirm-window.tsx"),
      "utf8",
    );
    expect(window.match(/"danger"/g)).toHaveLength(1);
    expect(window).toContain('variant={danger ? "danger" : "primary"}');
  });

  test("figures do not jitter: the page uses tabular numbers, and the ages are monospace", () => {
    expect(code).toMatch(/\.rg-page \{\s*font-variant-numeric: tabular-nums/);
    expect(code).toMatch(/\.rg-age \{[^}]*font-family: var\(--ds-font-mono\)/);
  });
});
