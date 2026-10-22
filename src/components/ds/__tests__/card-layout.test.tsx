/**
 * Layout audit 2026-10-21, P3: the card footer slot, the card grid and the
 * full-width panel.
 */
import { setupDom, render } from "./dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Card } from "../Display";

setupDom();

const ds = readFileSync(
  join(import.meta.dir, "../../../styles/ds.css"),
  "utf8",
);
const block = (selector: string) => {
  const at = ds.indexOf(`\n${selector} {`);
  expect(at).toBeGreaterThan(-1);
  return ds.slice(at, ds.indexOf("}", at) + 1);
};

describe("Card footer", () => {
  test("a footer renders last, in its own slot; without one there is no slot", async () => {
    const { host } = await render(
      <>
        <Card title="With" footer={<button type="button">All</button>}>
          <p>body</p>
        </Card>
        <Card title="Without">
          <p>body</p>
        </Card>
      </>,
    );
    const [withFooter, without] = host.querySelectorAll("section.ds-card");
    const last = withFooter.lastElementChild as HTMLElement;
    expect(last.tagName).toBe("FOOTER");
    expect(last.className).toBe("ds-card__footer");
    expect(last.textContent).toBe("All");
    expect(without.querySelector(".ds-card__footer")).toBeNull();
  });

  test("a card with a footer is a column and the footer is pushed to the bottom", () => {
    expect(block(".ds-card:has(> .ds-card__footer)")).toMatch(
      /flex-direction:\s*column/,
    );
    expect(block(".ds-card__footer")).toMatch(/margin-top:\s*auto/);
  });
});

describe("card grid and panel", () => {
  test("the grid wraps at --ds-card-min and stretches every card in a row", () => {
    const grid = block(".ds-card-grid");
    expect(grid).toMatch(/minmax\(min\(var\(--ds-card-min\), 100%\), 1fr\)/);
    expect(grid).toMatch(/align-items:\s*stretch/);
    expect(grid).toMatch(/gap:\s*var\(--ds-space-4\)/);
  });

  test("a panel is 12/16 inside with an md radius; a state edge is an inset shadow, never a border width", () => {
    const panel = block(".ds-panel");
    expect(panel).toMatch(/padding:\s*var\(--ds-space-3\) var\(--ds-space-4\)/);
    expect(panel).toMatch(/border-radius:\s*var\(--ds-radius-md\)/);
    for (const tone of ["info", "success", "warning", "danger"]) {
      const rule = block(`.ds-panel--${tone}`);
      expect(rule).toMatch(
        new RegExp(`box-shadow:\\s*inset [^;]*var\\(--ds-${tone}\\)`),
      );
      expect(rule).not.toMatch(/border|padding|margin/);
    }
  });
});
