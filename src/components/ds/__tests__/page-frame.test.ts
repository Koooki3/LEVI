/**
 * Layout audit 2026-10-21, P1 (user: two page widths, one set of margins):
 * every page frame is .ds-page; no page keeps a width or frame padding of its
 * own, so moving between pages never shifts the content's left edge.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const src = join(import.meta.dir, "../../..");
const read = (rel: string) => readFileSync(join(src, rel), "utf8");
const ds = read("styles/ds.css");
const tokens = read("styles/tokens.css");

function block(css: string, selector: string): string {
  const at = css.indexOf(`\n${selector} {`);
  expect(at).toBeGreaterThan(-1);
  return css.slice(at, css.indexOf("}", at) + 1);
}

describe("the page frame", () => {
  test("two widths and one set of margins are tokens", () => {
    expect(tokens).toMatch(/--ds-page-max-reading:\s*1200px/);
    expect(tokens).toMatch(/--ds-page-max-wide:\s*1360px/);
    expect(tokens).toMatch(/--ds-page-inset:\s*var\(--ds-space-6\)/);
    expect(tokens).toMatch(/--ds-page-top:\s*var\(--ds-space-8\)/);
    expect(tokens).toMatch(/--ds-page-bottom:\s*var\(--ds-space-16\)/);
  });

  test(".ds-page is wide by default, .ds-page--reading narrow, the inset drops to 16 where the header's does", () => {
    expect(block(ds, ".ds-page")).toMatch(
      /--ds-page-max:\s*var\(--ds-page-max-wide\)/,
    );
    expect(block(ds, ".ds-page--reading")).toMatch(
      /--ds-page-max:\s*var\(--ds-page-max-reading\)/,
    );
    // one width for both (supervisor 10-21): at 900-999 px the header was at
    // 16 and the page at 24, an 8 px jump of the left edge
    const page = ds.match(
      /@media \(max-width: (\d+)px\) \{\s*\.ds-page \{\s*--ds-page-inset:\s*var\(--ds-space-4\);/,
    );
    const shell = read("styles/shell.css").match(
      /@media \(max-width: (\d+)px\) \{\s*:root \{\s*--levi-header-height:[\s\S]*?\.levi-shell-header \{[^}]*padding: 0 var\(--ds-space-4\);/,
    );
    expect(page?.[1]).toBeDefined();
    expect(page?.[1]).toBe(shell?.[1]);
  });

  test("the old frames keep no width or frame padding of their own", () => {
    for (const [file, selector] of [
      ["components/pages-ui/pages.css", ".pg-workbench"],
      ["styles/home.css", ".levi-home"],
      ["styles/reading.css", ".levi-reading"],
    ] as const) {
      const rule = block(read(file), selector);
      expect(rule).not.toMatch(/max-width|padding|--pg-max/);
    }
  });

  test("every page that used an old frame now carries .ds-page", () => {
    const pages: Record<string, RegExp> = {
      "components/home/home-dashboard.tsx": /className="ds-page ds-page--reading levi-home"/,
      "app/guide/page.tsx": /className="ds-page ds-page--reading levi-reading"/,
      "app/workbench/page.tsx": /className="ds-root ds-page pg-workbench"/,
      "app/live/page.tsx": /className="ds-root ds-page pg-workbench pg-live"/,
    };
    for (const [file, pattern] of Object.entries(pages))
      expect(read(file)).toMatch(pattern);
  });
});
