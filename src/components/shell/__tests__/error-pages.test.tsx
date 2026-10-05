import { click, flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import { mock } from "bun:test";

mock.module("next/navigation", () => ({
  usePathname: () => "/no/such/page/here",
  useRouter: () => ({ push: () => {}, replace: () => {} }),
}));
const { NotFoundPage, RouteErrorPage } = await import("../error-pages");
const { RouteTitle } = await import("../route-title");

setupDom();

describe("not-found and error pages", () => {
  test("404 says what, why and what to do, with a way home", async () => {
    const { host } = await render(<NotFoundPage />);
    expect(host.textContent).toContain("This page does not exist");
    const links = [...host.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/", "/explore"]);
    expect(links[0].textContent).toContain("Back to the home page");
    expect(links[0].className).toContain("ds-btn--primary");
    expect(host.querySelector("main.levi-error-page")).toBeTruthy();
  });

  test("an uncaught error offers Try again and keeps the message collapsed", async () => {
    let retried = 0;
    const { host } = await render(
      <RouteErrorPage message="boom: half a file" onRetry={() => retried++} />,
    );
    expect(host.querySelector("[role=alert]")).toBeTruthy();
    expect(host.querySelector("details pre")?.textContent).toBe(
      "boom: half a file",
    );
    const retry = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Try again"),
    );
    await click(retry!);
    expect(retried).toBe(1);
    expect(host.querySelector('a[href="/"]')).toBeTruthy();
  });

  test("the routes exist and the page fills what is below the bar", () => {
    const app = join(import.meta.dir, "../../../app");
    for (const file of ["not-found.tsx", "error.tsx", "global-error.tsx"])
      expect(readFileSync(join(app, file), "utf8").length).toBeGreaterThan(0);
    const css = readFileSync(join(app, "../styles/shell.css"), "utf8");
    expect(css).toMatch(
      /\.levi-error-page \{[^}]*min-height:\s*calc\(100dvh - var\(--levi-header-height/,
    );
    // The default framework page is not what answers a missing address.
    expect(readFileSync(join(app, "not-found.tsx"), "utf8")).toContain(
      "NotFoundPage",
    );
  });

  test("the page keeps its main landmark; only the words are an alert, and focus lands on them", async () => {
    const { host } = await render(
      <RouteErrorPage message="boom" onRetry={() => {}} />,
    );
    const main = host.querySelector("main")!;
    expect(main.hasAttribute("role")).toBe(false);
    const title = host.querySelector(".levi-error-page__title")!;
    // Focus moving to the headline already reads it: it is no alert itself,
    // the message is.
    expect(title.hasAttribute("role")).toBe(false);
    expect(
      host.querySelector(".ds-empty__description [role=alert]"),
    ).toBeTruthy();
    expect(document.activeElement).toBe(title);
    const nf = await render(<NotFoundPage />);
    // A missing address is not an incident: focus moves, nothing is announced.
    expect(nf.host.querySelector("[role=alert]")).toBeNull();
    expect(nf.host.querySelector("main")!.hasAttribute("role")).toBe(false);
    expect(document.activeElement).toBe(
      nf.host.querySelector(".levi-error-page__title"),
    );
  });

  test("the tab title says so, and goes back when the page goes", async () => {
    const { rerender } = await render(
      <>
        <RouteTitle />
      </>,
    );
    await flush();
    expect(document.title).toBe("LEVI");
    await rerender(
      <>
        <RouteTitle />
        <NotFoundPage />
      </>,
    );
    await flush();
    expect(document.title).toBe("This page does not exist · LEVI");
    await rerender(
      <>
        <RouteTitle />
      </>,
    );
    await flush();
    expect(document.title).toBe("LEVI");
  });
});
