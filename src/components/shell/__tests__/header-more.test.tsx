import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";

// The header with its neighbours stubbed: what is under test is which links
// the bar keeps at a width and what the "More" menu holds.
const route = { path: "/marvel_workstation" };
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

const { NAV_COLLAPSE_WIDTHS, NAV_COMPACT_BELOW, NAV_ROW_BELOW } =
  await import("../commands");
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
  route.path = "/marvel_workstation";
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
const menuItems = async (host: HTMLElement) => {
  await click(moreTrigger(host));
  return Array.from(host.querySelectorAll("[role^=menuitem]"));
};
const REPORT = "Report";
const MAIN = [
  "/live",
  "/automatic",
  "/marvel_workstation",
  "/robot-studio",
  "/explore",
];
const texts = (host: HTMLElement) =>
  Array.from(host.querySelectorAll("[role^=menuitem]")).map(
    (item) => item.textContent,
  );

describe("the header navigation", () => {
  test("wide: every page but Report is a link, marvel_workstation and FR3 3D among them", async () => {
    atWidth(1920);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual([...MAIN, "/workbench", "/pool", "/guide"]);
    // marvel_workstation is the current page and says so.
    expect(
      host
        .querySelector("nav a[href='/marvel_workstation']")!
        .getAttribute("aria-current"),
    ).toBe("page");
    // Report is in More.
    await click(moreTrigger(host));
    expect(texts(host)).toEqual([REPORT]);
  });

  test("a little narrower: Guide moves into More, which lists it before Report", async () => {
    atWidth(1700);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual([...MAIN, "/workbench", "/pool"]);
    await click(moreTrigger(host));
    expect(texts(host)).toEqual(["Guide", REPORT]);
  });

  test("then the training pool, then Conversion & review", async () => {
    atWidth(1600);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual([...MAIN, "/workbench"]);
    await click(moreTrigger(host));
    expect(texts(host)).toEqual(["Training pool", "Guide", REPORT]);
  });

  test("a laptop width (1440 px): the five main links, marvel_workstation and FR3 3D among them, and everything else in More", async () => {
    atWidth(1440);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual(MAIN);
    await click(moreTrigger(host));
    expect(texts(host)).toEqual([
      "Conversion & review",
      "Training pool",
      "Guide",
      REPORT,
    ]);
  });

  test("narrowest one-row bar: the five main links stay; More holds the other four and chooses by the router", async () => {
    atWidth(NAV_ROW_BELOW);
    const { host } = await render(header());
    expect(hrefs(host)).toEqual(MAIN);
    await click(moreTrigger(host));
    const items = Array.from(host.querySelectorAll("[role^=menuitem]"));
    expect(items.map((item) => item.textContent)).toEqual([
      "Conversion & review",
      "Training pool",
      "Guide",
      REPORT,
    ]);
    await click(items[2]);
    expect(went).toEqual(["/guide"]);
  });

  test("the folding widths of the module are the ones the styles use, and each step shows its link from its own width", async () => {
    const { first, second, third } = NAV_COLLAPSE_WIDTHS;
    for (const [width, expected] of [
      [first, ["/workbench", "/pool", "/guide"]],
      [first - 1, ["/workbench", "/pool"]],
      [second, ["/workbench", "/pool"]],
      [second - 1, ["/workbench"]],
      [third, ["/workbench"]],
      [third - 1, []],
    ] as const) {
      atWidth(width);
      const { host } = await render(header());
      expect([width, hrefs(host).filter((h) => !MAIN.includes(h!))]).toEqual([
        width,
        [...expected],
      ]);
    }
  });

  test("the menu marks the current page when it is folded", async () => {
    atWidth(NAV_ROW_BELOW);
    route.path = "/guide";
    const { host } = await render(header());
    expect(host.querySelector(".levi-shell-more.is-current")).toBeTruthy();
    await click(moreTrigger(host));
    const checked = Array.from(
      host.querySelectorAll("[role=menuitemradio][aria-checked=true]"),
    ).map((item) => item.textContent);
    expect(checked).toEqual(["Guide"]);
  });

  test("two rows below NAV_ROW_BELOW: every link but Report is in the bar", async () => {
    for (const width of [600, NAV_ROW_BELOW - 1]) {
      atWidth(width);
      const { host } = await render(header());
      expect([width, hrefs(host)]).toEqual([
        width,
        [...MAIN, "/workbench", "/pool", "/guide"],
      ]);
    }
  });

  test("More is an icon-only button on the two-row bar (so the first links keep their room) and a labelled one from NAV_ROW_BELOW", async () => {
    for (const [width, iconOnly] of [
      [360, true],
      [420, true],
      [999, true],
      [NAV_ROW_BELOW - 1, true],
      [NAV_ROW_BELOW, false],
      [1440, false],
      [1920, false],
    ] as const) {
      atWidth(width);
      const { host } = await render(header());
      const trigger = moreTrigger(host);
      expect([
        width,
        trigger.classList.contains("ds-menu-trigger--icon"),
      ]).toEqual([width, iconOnly]);
      // The name stays "More" either way (text, hidden from sight when icon-only).
      expect([width, trigger.textContent]).toEqual([width, "More"]);
      expect([width, Boolean(trigger.querySelector(".ds-btn__label"))]).toEqual(
        [width, !iconOnly],
      );
    }
  });

  test("the FR3 3D link is in the bar at every width, reads FR3 3D (the palette keeps the full name), never folds, and is not in More", async () => {
    for (const width of [420, 999, 1170, 1359, 1360, 1440, 1530, 1700, 1920]) {
      atWidth(width);
      const { host } = await render(header());
      const link = host.querySelector("nav a[href='/robot-studio']");
      expect([width, link?.textContent]).toEqual([width, "FR3 3D"]);
      expect([width, link?.hasAttribute("data-collapse")]).toEqual([
        width,
        false,
      ]);
      await menuItems(host);
      for (const name of ["FR3 3D", "FR3 interactive preview"])
        expect([width, texts(host).includes(name)]).toEqual([width, false]);
    }
  });

  test("Report is a menu item at every width, never a link of the bar, and the menu goes to it", async () => {
    for (const width of [420, 999, 1170, 1440, 1530, 1700, 1920]) {
      atWidth(width);
      const { host } = await render(header());
      expect([width, hrefs(host).includes("/report")]).toEqual([width, false]);
      const items = await menuItems(host);
      const report = items.find((item) => item.textContent === REPORT);
      expect([width, Boolean(report)]).toEqual([width, true]);
      await click(report!);
      expect([width, went.pop()]).toEqual([width, "/report"]);
    }
  });

  test("on the Report page the menu marks it, and marvel_workstation is not marked", async () => {
    atWidth(1920);
    route.path = "/report";
    const { host } = await render(header());
    expect(host.querySelector(".levi-shell-more.is-current")).toBeTruthy();
    expect(
      host
        .querySelector("nav a[href='/marvel_workstation']")!
        .getAttribute("aria-current"),
    ).toBeNull();
    await click(moreTrigger(host));
    const checked = Array.from(
      host.querySelectorAll("[role=menuitemradio][aria-checked=true]"),
    ).map((item) => item.textContent);
    expect(checked).toEqual([REPORT]);
  });

  test("on the preview page its bar link is marked and More is not", async () => {
    atWidth(1440);
    route.path = "/robot-studio";
    const { host } = await render(header());
    expect(
      host
        .querySelector("nav a[href='/robot-studio']")!
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(host.querySelector(".levi-shell-more.is-current")).toBeNull();
  });

  test("marvel_workstation is a link of the bar at every width, never only in More, and is marked as the current page there", async () => {
    for (const width of [
      360, 420, 820, 999, 1000, 1169, 1170, 1240, 1359, 1360, 1440, 1530, 1640,
      1710, 1920,
    ]) {
      atWidth(width);
      const { host } = await render(header());
      const link = host.querySelector("nav a[href='/marvel_workstation']");
      expect([width, link?.getAttribute("aria-current")]).toEqual([
        width,
        "page",
      ]);
      // The menu never holds it (it would be the second place to look).
      if (moreTrigger(host)) {
        await click(moreTrigger(host));
        expect([width, texts(host).includes("marvel_workstation")]).toEqual([
          width,
          false,
        ]);
      }
    }
  });

  test("the link reads marvel_workstation and nothing after it; no link has a hint word", async () => {
    atWidth(1440);
    const { host } = await render(header());
    const link = host.querySelector("nav a[href='/marvel_workstation']")!;
    expect(link.textContent).toBe("marvel_workstation");
    expect(host.querySelector(".levi-shell-link__hint")).toBeNull();
    // The old addresses are not links any more (next.config.ts redirects them).
    expect(host.querySelector("nav a[href='/marvel0']")).toBeNull();
    expect(host.querySelector("nav a[href='/rig']")).toBeNull();
  });

  test("on the two-row bar the menu opens leftwards from the end of the row, so its panel stays on the screen", () => {
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/shell.css"),
      "utf8",
    );
    expect(css).toMatch(
      new RegExp(
        `@media \\(max-width: ${NAV_ROW_BELOW - 1}px\\) \\{[\\s\\S]*?\\.levi-shell-more \\.ds-menu--start \\{\\s*left: auto;\\s*right: 0;`,
      ),
    );
  });

  test("the compact bar (tight links, search icon only) ends where the full one starts", () => {
    expect(NAV_COMPACT_BELOW).toBeGreaterThan(NAV_ROW_BELOW);
  });

  test("the folded pages are still reached from More at a laptop width; marvel_workstation stays in the bar", async () => {
    atWidth(1200);
    route.path = "/guide";
    const { host } = await render(header());
    expect(hrefs(host)).toContain("/marvel_workstation");
    expect(host.querySelector(".levi-shell-more.is-current")).toBeTruthy();
  });
});
