/**
 * Layout audit 2026-10-21, P2: one page head. Title and tools on one row,
 * centred; no margin on the h1, also inside a workbench page whose
 * `.pg-workbench h1` rule sets margins on every page title.
 */
import { setupDom, render } from "./dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { PageHead } from "../PageHead";

setupDom();

const read = (rel: string) => readFileSync(join(import.meta.dir, rel), "utf8");
const ds = read("../../../styles/ds.css");
const pages = read("../../pages-ui/pages.css");
const rule = (css: string, selector: string) => {
  const at = css.indexOf(`\n${selector} {`);
  expect(at).toBeGreaterThan(-1);
  return css.slice(at + 1, css.indexOf("}", at) + 1);
};

describe("PageHead", () => {
  test("title, then the tools (meta before actions), then the lead", async () => {
    const { host } = await render(
      <PageHead
        title="Pool"
        meta={<span id="m">fresh</span>}
        actions={<button type="button">New</button>}
        lead="What this page is for."
      />,
    );
    const head = host.querySelector("header.ds-page-head") as HTMLElement;
    const row = head.querySelector(".ds-page-head__row") as HTMLElement;
    expect(row.firstElementChild?.tagName).toBe("H1");
    const tools = row.querySelector(".ds-page-head__tools") as HTMLElement;
    expect([...tools.children].map((c) => c.tagName)).toEqual([
      "SPAN",
      "BUTTON",
    ]);
    expect(head.lastElementChild?.className).toBe("ds-page-head__lead");
  });

  test("no tools and no lead render no empty boxes", async () => {
    const { host } = await render(<PageHead title="Live" />);
    expect(host.querySelector(".ds-page-head__tools")).toBeNull();
    expect(host.querySelector(".ds-page-head__lead")).toBeNull();
  });

  test("the row centres title and tools; computed: the h1 keeps no margin inside a workbench page", () => {
    expect(rule(ds, ".ds-page-head__row")).toMatch(/align-items:\s*center/);
    const css = [
      rule(ds, ".ds-page-head .ds-page-head__title"),
      rule(pages, ".pg-workbench h1"),
    ]
      .join("\n")
      .replaceAll(/var\(--[a-z0-9-]+\)/g, "4px");
    document.head.innerHTML = `<style>${css}</style>`;
    document.body.innerHTML = `<main class="pg-workbench"><header class="ds-page-head"><div class="ds-page-head__row"><h1 id="t" class="ds-page-head__title">T</h1></div></header></main>`;
    const h1 = getComputedStyle(document.getElementById("t") as HTMLElement);
    expect(h1.marginTop).toBe("0px");
    expect(h1.marginBottom).toBe("0px");
  });
});
