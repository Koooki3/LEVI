import { click, focus, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { SkipToContent } from "../skip-to-content";

setupDom();

describe("skip to main content", () => {
  test("is a link that moves focus to the page's main element", async () => {
    await render(
      <>
        <SkipToContent />
        <header>
          <button type="button">bar</button>
        </header>
        <main id="page">content</main>
      </>,
    );
    const link = document.querySelector<HTMLAnchorElement>("a.levi-skip")!;
    expect(link.textContent).toBe("Skip to main content");
    await focus(link);
    await click(link);
    expect(document.activeElement?.id).toBe("page");
    expect(document.querySelector("main")!.getAttribute("tabindex")).toBe("-1");
  });

  test("the target of href=#levi-main wins over an earlier main landmark", async () => {
    await render(
      <>
        <SkipToContent />
        <main id="other">somewhere else</main>
        <main id="levi-main">the page</main>
      </>,
    );
    expect(document.querySelector("a.levi-skip")!.getAttribute("href")).toBe(
      "#levi-main",
    );
    await click(document.querySelector("a.levi-skip"));
    expect(document.activeElement?.id).toBe("levi-main");
  });

  test("the pages named in the shell work mark their main as #levi-main, so the link also works without script", () => {
    const src = join(import.meta.dir, "../../..");
    const files = [
      "app/explore/explore-grid.tsx",
      "app/robot-studio/page.tsx",
      "components/report/report-view.tsx",
      "components/shell/error-pages.tsx",
      "app/global-error.tsx",
    ];
    for (const file of files) {
      const code = readFileSync(join(src, file), "utf8");
      expect(code).toMatch(/<main\s+id="levi-main"/);
    }
  });

  test("finds a role=main area", async () => {
    await render(
      <>
        <SkipToContent />
        <div role="main" id="specimen">
          content
        </div>
      </>,
    );
    await click(document.querySelector("a.levi-skip"));
    expect(document.activeElement?.id).toBe("specimen");
  });

  test("falls back to the viewer's content when there is no main", async () => {
    await render(
      <>
        <SkipToContent />
        <div id="vw-main">viewer</div>
      </>,
    );
    await click(document.querySelector("a.levi-skip"));
    expect(document.activeElement?.id).toBe("vw-main");
  });

  test("every page has a main landmark, the specimen included, and main scrolls below the bar", () => {
    const specimen = readFileSync(
      join(import.meta.dir, "../../../app/design/specimen.tsx"),
      "utf8",
    );
    expect(specimen).toMatch(/<main ref=\{top\} className="ds-root dsp-page">/);
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/shell.css"),
      "utf8",
    );
    expect(css).toMatch(
      /\nmain \{[^}]*scroll-margin-top:\s*var\(--levi-sticky-top/,
    );
  });

  test("is the first thing in the frame, hidden until focused", () => {
    const layout = readFileSync(
      join(import.meta.dir, "../../../app/layout.tsx"),
      "utf8",
    );
    expect(layout.indexOf("<SkipToContent />")).toBeGreaterThan(0);
    expect(layout.indexOf("<SkipToContent />")).toBeLessThan(
      layout.indexOf("<LeviHeader />"),
    );
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/shell.css"),
      "utf8",
    );
    expect(css).toMatch(/\.levi-skip \{[^}]*clip-path: inset\(50%\)/);
    expect(css).toMatch(/\.levi-skip:focus \{[^}]*clip-path: none/);
  });
});
