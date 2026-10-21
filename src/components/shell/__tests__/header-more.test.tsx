import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

// The header with its neighbours stubbed: what is under test is which links
// the bar keeps at a width and what the "More" menu holds.
const route = { path: "/marvel0" };
const went: string[] = [];
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  usePathname: () => route.path,
}));
mock.module("@/components/live/use-live-pulse", () => ({
  useLivePulse: () => ({ enabled: false, embedded: false, pulse: null }),
}));
mock.module("@/components/live/live-nav", () => ({
  LiveNavLink: () => (
    // A stub of the real link (next/link is not under test): the tests read
    // its href from the rendered bar like any other link.
    // eslint-disable-next-line @next/next/no-html-link-for-pages
    <a className="levi-live-nav" href="/live">
      Live evaluation
    </a>
  ),
}));
mock.module("@/components/shell/jobs-menu", () => ({ JobsMenu: () => null }));
mock.module("@/components/shell/command-palette", () => ({
  CommandPalette: () => null,
}));
mock.module("@/components/shell/shortcuts-dialog", () => ({
  ShortcutsDialog: () => null,
}));
mock.module("@/components/shell/shell-events", () => ({
  SHELL_EVENTS: {
    agentToggle: "levi-agent-toggle",
    agentConnections: "levi-agent-connections",
    agentState: "levi-agent-state",
    go: "levi-go",
  },
  requestGo: (href: string) => went.push(href),
  toggleAgentWorkbench: () => undefined,
  openAgentConnections: () => undefined,
}));

const { ShellProvider } = await import("../shell-context");
const { default: LeviHeader } = await import("../../levi-header");

setupDom();

const original = window.innerWidth;
function atWidth(width: number) {
  Object.defineProperty(window, "innerWidth", {
    value: width,
    configurable: true,
  });
}
afterEach(() => {
  atWidth(original);
  route.path = "/marvel0";
  went.length = 0;
});

const hrefs = (host: HTMLElement) =>
  Array.from(host.querySelectorAll("nav a")).map((a) => a.getAttribute("href"));
const header = () => (
  <ShellProvider>
    <LeviHeader />
  </ShellProvider>
);
const moreTrigger = (host: HTMLElement) =>
  host.querySelector<HTMLButtonElement>(".levi-shell-more__trigger")!;

describe("the header navigation", () => {
  test("wide: every page is a link, marvel0 among them", async () => {
    atWidth(1700);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual([
      "/live",
      "/automatic",
      "/marvel0",
      "/explore",
      "/workbench",
      "/pool",
      "/guide",
      "/report",
    ]);
    // marvel0 is the current page and says so.
    expect(
      host
        .querySelector("nav a[href='/marvel0']")!
        .getAttribute("aria-current"),
    ).toBe("page");
  });

  test("a little narrower: Guide and Report move into More, which lists them", async () => {
    atWidth(1530);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual([
      "/live",
      "/automatic",
      "/marvel0",
      "/explore",
      "/workbench",
      "/pool",
    ]);
    await click(moreTrigger(host));
    const items = Array.from(host.querySelectorAll("[role^=menuitem]")).map(
      (item) => item.textContent,
    );
    expect(items).toEqual(["Guide", "Report"]);
  });

  test("a laptop width (1440 px): the four main links, marvel0 among them, and everything else in More", async () => {
    atWidth(1440);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual([
      "/live",
      "/automatic",
      "/marvel0",
      "/explore",
    ]);
    await click(moreTrigger(host));
    expect(
      Array.from(host.querySelectorAll("[role^=menuitem]")).map(
        (item) => item.textContent,
      ),
    ).toEqual(["Conversion & review", "Training pool", "Guide", "Report"]);
  });

  test("narrow: only the four main links stay; More holds the other four and chooses by the router", async () => {
    atWidth(1000);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual([
      "/live",
      "/automatic",
      "/marvel0",
      "/explore",
    ]);
    await click(moreTrigger(host));
    const items = Array.from(host.querySelectorAll("[role^=menuitem]"));
    expect(items.map((item) => item.textContent)).toEqual([
      "Conversion & review",
      "Training pool",
      "Guide",
      "Report",
    ]);
    await click(items[2]);
    expect(went).toEqual(["/guide"]);
  });

  test("the menu marks the current page when it is folded", async () => {
    atWidth(1000);
    route.path = "/guide";
    const { host } = await render(header());
    expect(host.querySelector(".levi-shell-more.is-current")).toBeTruthy();
    await click(moreTrigger(host));
    const checked = Array.from(
      host.querySelectorAll("[role=menuitemradio][aria-checked=true]"),
    ).map((item) => item.textContent);
    expect(checked).toEqual(["Guide"]);
  });

  test("two rows below 1000 px: every link is in the bar", async () => {
    atWidth(600);
    const { host } = await render(header());
    expect(hrefs(host)).toHaveLength(8);
  });

  test("marvel0 is a link of the bar at every width, never only in More, and is marked as the current page there", async () => {
    for (const width of [
      360, 420, 820, 999, 1000, 1024, 1240, 1440, 1560, 1700,
    ]) {
      atWidth(width);
      const { host } = await render(header());
      const link = host.querySelector("nav a[href='/marvel0']");
      expect([width, link?.getAttribute("aria-current")]).toEqual([
        width,
        "page",
      ]);
      // The menu never holds it (it would be the second place to look).
      if (moreTrigger(host)) {
        await click(moreTrigger(host));
        const items = Array.from(host.querySelectorAll("[role^=menuitem]")).map(
          (item) => item.textContent,
        );
        expect([width, items.includes("MARVEL0")]).toEqual([width, false]);
      }
    }
  });

  test("the link reads MARVEL0 with 'robot' after it, a word the stylesheet drops where the bar is short of room", async () => {
    atWidth(1440);
    const { host } = await render(header());
    const link = host.querySelector("nav a[href='/marvel0']")!;
    expect(link.textContent).toBe("MARVEL0robot");
    const hint = link.querySelector(".levi-shell-link__hint")!;
    expect(hint.textContent).toBe("robot");
    // The accessible name keeps the hint (it is text, not decoration); the
    // other links have none.
    expect(
      host.querySelector("nav a[href='/explore'] .levi-shell-link__hint"),
    ).toBeNull();
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/shell.css"),
      "utf8",
    );
    expect(css).toMatch(
      /@media \(max-width: 1239px\) \{\s*\.levi-shell-link__hint \{\s*display: none/,
    );
  });

  test("the folded pages are still reached from More at a laptop width; marvel0 stays in the bar", async () => {
    atWidth(1024);
    route.path = "/guide";
    const { host } = await render(header());
    expect(hrefs(host)).toContain("/marvel0");
    expect(host.querySelector(".levi-shell-more.is-current")).toBeTruthy();
  });
});
