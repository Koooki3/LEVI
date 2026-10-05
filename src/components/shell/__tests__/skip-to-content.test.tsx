import {
  click,
  focus,
  press,
  render,
  setupDom,
} from "@/components/ds/__tests__/dom";
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
