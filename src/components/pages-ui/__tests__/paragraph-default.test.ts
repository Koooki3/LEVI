/**
 * Layout audit 2026-10-21, P0: `.pg-workbench p` (one class + one element,
 * 0,1,1) overrode every one-class paragraph style on a page: a field error
 * lost its danger colour, an empty-state title its size. The page default is
 * now `:where(.pg-workbench) p` (0,0,1).
 */
import "../../ds/__tests__/dom-register";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const read = (rel: string) =>
  readFileSync(join(import.meta.dir, rel), "utf8");
const pages = read("../pages.css");
const ds = read("../../../styles/ds.css");

/** The first rule of `css` whose selector list is exactly `selector`. */
function rule(css: string, selector: string): string {
  const at = css.indexOf(`\n${selector} {`);
  expect(at).toBeGreaterThan(-1);
  return css.slice(at + 1, css.indexOf("}", at) + 1);
}

describe("the page default for paragraphs", () => {
  test("no page-wide paragraph rule outranks a one-class rule", () => {
    // any `.pg-workbench … p` rule with a bare class in front is 0,1,1 again
    expect(pages).not.toMatch(/(^|\n)\.pg-workbench p\s*[,{]/);
    expect(pages).toContain(":where(.pg-workbench) p {");
  });

  test("computed: a field error is danger, an empty title is title-3, a bare paragraph keeps the page default", () => {
    // The real rules, with every token they use replaced by a distinct literal.
    const tokens = {
      "--ds-text-callout-size": "13px",
      "--ds-text-callout-line": "18px",
      "--ds-text-secondary": "rgb(1, 2, 3)",
      "--ds-text-caption-size": "12px",
      "--ds-text-caption-line": "16px",
      "--ds-danger": "rgb(200, 0, 0)",
      "--ds-text-title-3-size": "16px",
      "--ds-text-title-3-line": "22px",
      "--ds-weight-semibold": "600",
    };
    // in the order pages load them: ds.css first, pages.css after it (a later
    // rule of equal specificity would win, so the page default goes last)
    let css = [
      rule(ds, ".ds-field__error"),
      rule(ds, ".ds-empty__title"),
      rule(pages, ":where(.pg-workbench) p"),
    ].join("\n");
    for (const [name, value] of Object.entries(tokens))
      css = css.replaceAll(`var(${name})`, value);
    document.head.innerHTML = `<style>${css}</style>`;
    document.body.innerHTML = `<main class="pg-workbench">
      <p id="bare">plain</p>
      <p id="err" class="ds-field__error">bad</p>
      <p id="title" class="ds-empty__title">empty</p>
      <p id="other" class="no-own-rule">other</p>
    </main>`;
    // first real run: if "bare" fails, check that happy-dom supports :where()
    // before suspecting the rule (supervisor 10-21)
    const style = (id: string) =>
      getComputedStyle(document.getElementById(id) as HTMLElement);
    expect(style("err").color).toBe("rgb(200, 0, 0)");
    expect(style("err").fontSize).toBe("12px");
    expect(style("title").fontSize).toBe("16px");
    expect(style("bare").fontSize).toBe("13px");
    expect(style("bare").color).toBe("rgb(1, 2, 3)");
    // a class with no size or colour of its own keeps the page default
    expect(style("other").fontSize).toBe("13px");
    expect(style("other").color).toBe("rgb(1, 2, 3)");
  });
});
