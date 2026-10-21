import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

// The stylesheet of Step 5, read as text: the rules a render in a test DOM
// cannot show (no cascade, no layout).

const here = import.meta.dir;
const css = readFileSync(join(here, "../autonomy-styles.css"), "utf8");
const code = css.replace(/\/\*[\s\S]*?\*\//g, "");
const tokens = readFileSync(join(here, "../../../styles/tokens.css"), "utf8");
const shell = readFileSync(join(here, "../../../styles/shell.css"), "utf8");
const source = (file: string) => readFileSync(join(here, "..", file), "utf8");

describe("the stylesheet of the autonomy step", () => {
  test("no colour is written out: every colour is a design token", () => {
    expect(code.match(/#[0-9a-fA-F]{3,8}\b/g)).toBeNull();
    expect(
      code.match(/\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklch)\(/g),
    ).toBeNull();
  });

  test("every variable it uses is a --ds-* token the design system defines", () => {
    const used = [...code.matchAll(/var\((--[a-z0-9-]+)/g)].map((m) => m[1]);
    expect(used.length).toBeGreaterThan(30);
    for (const name of new Set(used)) {
      // The one exception: where the shell's sticky bar ends (shell.css).
      if (name === "--levi-sticky-top") {
        expect(shell.includes(`${name}:`)).toBe(true);
        continue;
      }
      expect(name.startsWith("--ds-")).toBe(true);
      expect([name, tokens.includes(`${name}:`)]).toEqual([name, true]);
    }
  });

  test("Stop is 64 px high and the full width of its frame; a key is at least 48 px, on a phone as well", () => {
    expect(code).toMatch(/\.rg-arun \.rg-astop__btn \{[^}]*width: 100%/);
    expect(code).toMatch(/\.rg-arun \.rg-astop__btn \{[^}]*min-height: 64px/);
    expect(code).toMatch(/\.rg-akey \.rg-akey__btn \{[^}]*min-height: 48px/);
    expect(code).toMatch(
      /@media \(max-width: 639px\)[\s\S]*\.rg-arun \.ds-btn,\s*\.rg-aprepare \.ds-btn \{\s*min-height: 48px/,
    );
  });

  test("on a phone Stop stays at the top of the card while the card scrolls", () => {
    expect(code).toMatch(
      /@media \(max-width: 639px\)[\s\S]*\.rg-arun \.rg-astop \{\s*position: sticky;\s*top: calc\(var\(--levi-sticky-top, 56px\) \+ var\(--ds-space-2\)\)/,
    );
  });

  test("the keys are two columns only where there is room and a second step to show; one column on a phone", () => {
    expect(code).toMatch(
      /@media \(min-width: 720px\) \{\s*\.rg-akeys__cols--two \{\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/,
    );
    expect(code).toMatch(
      /\.rg-akeys__cols \{[^}]*grid-template-columns: minmax\(0, 1fr\)/,
    );
  });

  test("an arm that moves is marked by a border AND words AND an icon, never by colour alone", () => {
    expect(code).toMatch(
      /\.rg-akey \.rg-akey__btn--moves \{[^}]*border-left: 4px solid var\(--ds-warning\)/,
    );
    const keys = source("autonomy-keys.tsx");
    expect(keys).toContain("rg-akey__btn--moves");
    expect(keys).toMatch(/<TriangleAlert[^>]*aria-hidden="true"/);
    expect(keys).toContain("marvel0-autonomy-moves-");
  });

  test("nothing moves by itself: no animation, no transition", () => {
    expect(code).not.toMatch(/animation|@keyframes|transition/);
  });

  test("the objects table becomes stacked rows on a phone, each cell labelled", () => {
    expect(code).toMatch(
      /@media \(max-width: 639px\)[\s\S]*\.rg-atable td::before \{\s*content: attr\(data-label\)/,
    );
    expect(source("autonomy-run-card.tsx")).toContain("data-label=");
  });
});

describe("the dock's reason for a Stop that is off (SAFETY N2)", () => {
  test("a phone keeps it: the dock's usual note is hidden there, this one is shown", () => {
    const dock = readFileSync(join(here, "../marvel0-dock.css"), "utf8");
    // The shared rule that hides the dock's note on a phone ...
    expect(dock).toMatch(
      /@media \(max-width: 720px\)[\s\S]*\.rg-dock__note:not\(\.rg-dock__note--lost\) \{\s*display: none/,
    );
    // ... and the rule of this step that shows the reason regardless.
    expect(code).toMatch(
      /@media \(max-width: 720px\) \{\s*\.rg-page \.rg-dock \.rg-dock__note--why \{\s*display: block/,
    );
    expect(source("autonomy-step.tsx")).toContain("rg-dock__note--why");
  });

  test("a Stop that is off does not stick to the top of a phone: its reason is long and it has nothing to press", () => {
    expect(code).toMatch(
      /\.rg-arun \.rg-astop\[data-mode="off"\],\s*\.rg-arun \.rg-astop\[data-mode="unoffered"\] \{\s*position: static/,
    );
  });
});

describe("the one solid danger button", () => {
  test("Stop is the only one: in the card and, repeated, in the dock; no key, no start, no end is solid red", () => {
    const solid = (file: string) =>
      (source(file).match(/variant="danger"/g) ?? []).length;
    expect(solid("autonomy-run-card.tsx")).toBe(1);
    expect(solid("autonomy-step.tsx")).toBe(1);
    for (const file of ["autonomy-keys.tsx", "autonomy-prepare.tsx"])
      expect([file, solid(file)]).toEqual([file, 0]);
    // The confirm window of a stop that cuts something is red through `danger`.
    expect(source("autonomy-run-card.tsx")).toContain("danger\n");
  });
});
