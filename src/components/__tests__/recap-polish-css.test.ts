import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Layout cannot be measured in a test DOM, so these pin the rules the
// narrow-card fixes rely on (checked in a real browser at 390 and 1440px).
const css = readFileSync(
  join(import.meta.dir, "..", "viewer", "annotations.css"),
  "utf8",
);
const rule = (selector: string) => {
  const start = css.indexOf(selector + " {");
  expect(start).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start));
};

describe("recap value section layout rules", () => {
  test("axis-mode segments wrap onto a second row instead of being squeezed", () => {
    const group = rule(".annotations-skin .recap-axis-controls .ds-segmented");
    expect(group).toContain("flex-wrap: wrap");
    expect(group).toContain("max-width: 100%");
    const segment = rule(".annotations-skin .recap-axis-controls .ds-segment");
    expect(segment).toContain("white-space: nowrap");
    expect(segment).toContain("flex: 0 0 auto");
  });

  test("an advantage row is two label lines tall so wrapped names cannot overlap", () => {
    expect(
      rule(".annotations-skin .recap-section .tl-row.recap-advantage-row"),
    ).toContain("height: 32px");
  });

  test("bar value labels carry a halo so they stay legible over a neighbouring bar", () => {
    expect(css).toMatch(/text\.recap-bar-value \{[^}]*paint-order: stroke/);
  });

  test("blocks that are not tracks sit above the playhead line", () => {
    expect(css).toMatch(
      /\.recap-section \.recap-charts,[\s\S]*?\{[^}]*z-index: 6/,
    );
  });

  test("axis tick labels start clear of the playhead marker at the track start", () => {
    const left = /left: (\d+)px/.exec(rule(".annotations-skin .recap-axis"));
    expect(Number(left?.[1])).toBeGreaterThanOrEqual(10);
  });
});
