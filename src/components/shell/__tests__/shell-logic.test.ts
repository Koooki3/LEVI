import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  THEME_DEFAULT_PREFERENCE,
  THEME_STORAGE_KEY,
} from "@/lib/design/theme";
import {
  buildCommands,
  filterCommands,
  isCurrentPage,
  headerPages,
  NAV_COLLAPSE_WIDTHS,
  NAV_COMPACT_BELOW,
  NAV_ROW_BELOW,
  navCollapseLevel,
  navPages,
  splitHeaderPages,
} from "../commands";
import {
  globalShortcut,
  isApplePlatform,
  paletteKeys,
  shortcutGroups,
} from "../global-keys";
import { countConversionJobs, countPoolJobs, totalJobs } from "../jobs";
import {
  THEME_BOOT_DEFAULT,
  THEME_BOOT_KEY,
  THEME_BOOT_SCRIPT,
} from "../theme-boot";

const input = { closest: (selector: string) => (selector ? {} : null) };
const plain = { closest: () => null };

describe("global shortcuts", () => {
  test("Ctrl+K elsewhere, ⌘K on Apple, also from a text field", () => {
    expect(globalShortcut({ key: "k", ctrlKey: true }, false)).toBe("palette");
    expect(globalShortcut({ key: "K", ctrlKey: true }, false)).toBe("palette");
    expect(globalShortcut({ key: "k", metaKey: true }, false)).toBeNull();
    expect(globalShortcut({ key: "k", metaKey: true }, true)).toBe("palette");
    expect(globalShortcut({ key: "k", ctrlKey: true }, true)).toBeNull();
    expect(
      globalShortcut(
        { key: "k", ctrlKey: true, target: input as unknown as EventTarget },
        false,
      ),
    ).toBe("palette");
  });

  test("other modifiers and plain K do nothing", () => {
    expect(globalShortcut({ key: "k" }, false)).toBeNull();
    expect(
      globalShortcut({ key: "k", ctrlKey: true, shiftKey: true }, false),
    ).toBeNull();
    expect(
      globalShortcut({ key: "k", ctrlKey: true, altKey: true }, false),
    ).toBeNull();
  });

  test("? opens the list, but not while typing or with a modifier", () => {
    expect(
      globalShortcut(
        { key: "?", shiftKey: true, target: plain as unknown as EventTarget },
        false,
      ),
    ).toBe("shortcuts");
    expect(
      globalShortcut(
        { key: "?", shiftKey: true, target: input as unknown as EventTarget },
        false,
      ),
    ).toBeNull();
    expect(globalShortcut({ key: "?", ctrlKey: true }, false)).toBeNull();
  });

  test("? also as the full-width ？ and through AltGr (Ctrl+Alt)", () => {
    const at = plain as unknown as EventTarget;
    expect(globalShortcut({ key: "？", target: at }, false)).toBe("shortcuts");
    expect(
      globalShortcut(
        { key: "?", ctrlKey: true, altKey: true, target: at },
        false,
      ),
    ).toBe("shortcuts");
    expect(
      globalShortcut(
        { key: "？", target: input as unknown as EventTarget },
        false,
      ),
    ).toBeNull();
    expect(
      globalShortcut({ key: "?", altKey: true, target: at }, false),
    ).toBeNull();
    expect(
      globalShortcut({ key: "?", metaKey: true, target: at }, true),
    ).toBeNull();
  });

  test("never during IME composition", () => {
    expect(
      globalShortcut({ key: "k", ctrlKey: true, isComposing: true }, false),
    ).toBeNull();
    expect(globalShortcut({ key: "?", keyCode: 229 }, false)).toBeNull();
  });

  test("does not take the pages' keys", () => {
    for (const key of [" ", "ArrowUp", "ArrowDown", "j", "k", "Escape"])
      expect(globalShortcut({ key }, false)).toBeNull();
    for (const key of ["s", "z", "y"])
      expect(globalShortcut({ key, ctrlKey: true }, false)).toBeNull();
  });

  test("platform labels and the shortcut list", () => {
    expect(isApplePlatform("MacIntel")).toBe(true);
    expect(isApplePlatform("Linux x86_64")).toBe(false);
    expect(paletteKeys(true)).toEqual(["⌘", "K"]);
    expect(paletteKeys(false)).toEqual(["Ctrl", "K"]);
    const groups = shortcutGroups(false);
    expect(groups[0].rows[0].keys[0]).toEqual(["Ctrl", "K"]);
    expect(groups.flatMap((g) => g.rows.map((r) => r.label))).toContain(
      "Save the episode",
    );
  });
});

describe("navigation and commands", () => {
  test("marvel_workstation and the FR3 3D preview are header links; Report is a menu item; every page is offered by the header", () => {
    const pages = navPages({ live: true, pool: true });
    const all = pages.map((p) => p.href);
    expect(all).toContain("/marvel_workstation");
    expect(all).toContain("/robot-studio");
    expect(all).not.toContain("/marvel0");
    expect(headerPages(pages).map((p) => p.href)).toEqual(all);
    // marvel_workstation and the preview never fold and are never only menu
    // items; Report is only ever a menu item (a page for now and then must
    // not push a main link out).
    for (const href of ["/marvel_workstation", "/robot-studio"]) {
      const page = pages.find((p) => p.href === href)!;
      expect([href, page.menuOnly]).toEqual([href, undefined]);
      expect([href, page.collapse]).toEqual([href, undefined]);
    }
    const report = pages.find((p) => p.href === "/report")!;
    expect(report.menuOnly).toBe(true);
    expect(report.collapse).toBeUndefined();
    // The bar says "FR3 3D"; the palette and the page keep the full name.
    const preview = pages.find((p) => p.href === "/robot-studio")!;
    expect(preview.label).toBe("robotStudio.title");
    expect(preview.barLabel).toBe("robotStudio.nav");
    expect(en["robotStudio.nav"]).toBe("FR3 3D");
    expect(zh["robotStudio.nav"]).toBe("FR3 三维预览");
  });

  test("folding: nothing at full width, Guide first, then Training pool, then Conversion & review; Live, Automatic, marvel_workstation, FR3 3D and Explore never fold; Report is always in the menu", () => {
    const pages = navPages({ live: true, pool: true });
    const names = (list: Array<{ href: string }>) => list.map((p) => p.href);
    const wide = splitHeaderPages(pages, 0);
    expect(names(wide.more)).toEqual(["/report"]);
    expect(names(wide.links)).toEqual(
      names(pages).filter((href) => href !== "/report"),
    );
    expect(names(splitHeaderPages(pages, 1).more)).toEqual([
      "/guide",
      "/report",
    ]);
    expect(names(splitHeaderPages(pages, 2).more)).toEqual([
      "/pool",
      "/guide",
      "/report",
    ]);
    const third = splitHeaderPages(pages, 3);
    expect(names(third.more)).toEqual([
      "/workbench",
      "/pool",
      "/guide",
      "/report",
    ]);
    expect(names(third.links)).toEqual([
      "/live",
      "/automatic",
      "/marvel_workstation",
      "/robot-studio",
      "/explore",
    ]);
    // Every page is in the bar or in the menu, once, in navigation order.
    for (const level of [0, 1, 2, 3] as const) {
      const { links, more } = splitHeaderPages(pages, level);
      expect([...names(links), ...names(more)].sort()).toEqual(
        names(pages).sort(),
      );
    }
    // Without a pool the pool is not offered and nothing else changes.
    const noPool = splitHeaderPages(navPages({ live: true, pool: false }), 3);
    expect(names(noPool.more)).toEqual(["/workbench", "/guide", "/report"]);
  });

  test("the window width picks the folding level; the two-row bar below NAV_ROW_BELOW folds nothing", () => {
    const { first, second, third } = NAV_COLLAPSE_WIDTHS;
    expect(first).toBeGreaterThan(second);
    expect(second).toBeGreaterThan(third);
    expect(third).toBeGreaterThan(NAV_COMPACT_BELOW);
    expect(NAV_COMPACT_BELOW).toBeGreaterThan(NAV_ROW_BELOW);
    expect(navCollapseLevel(1920)).toBe(0);
    expect(navCollapseLevel(first)).toBe(0);
    expect(navCollapseLevel(first - 1)).toBe(1);
    expect(navCollapseLevel(second)).toBe(1);
    expect(navCollapseLevel(second - 1)).toBe(2);
    expect(navCollapseLevel(third)).toBe(2);
    expect(navCollapseLevel(third - 1)).toBe(3);
    expect(navCollapseLevel(NAV_ROW_BELOW)).toBe(3);
    expect(navCollapseLevel(NAV_ROW_BELOW - 1)).toBe(0);
    expect(navCollapseLevel(420)).toBe(0);
  });

  test("the styles hide the folded links and tighten the bar at the same widths", () => {
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/shell.css"),
      "utf8",
    );
    // Step 1 below `first`, step 2 below `second`, step 3 below `third`; the
    // links are tighter below NAV_COMPACT_BELOW; the bar has two rows (and
    // folds nothing) below NAV_ROW_BELOW.
    for (const [step, width] of [
      [1, NAV_COLLAPSE_WIDTHS.first],
      [2, NAV_COLLAPSE_WIDTHS.second],
      [3, NAV_COLLAPSE_WIDTHS.third],
    ] as const) {
      expect(css).toMatch(
        new RegExp(
          `@media \\(min-width: ${NAV_ROW_BELOW}px\\) and \\(max-width: ${width - 1}px\\) \\{\\s*\\.levi-shell-link\\[data-collapse="${step}"\\]`,
        ),
      );
    }
    expect(css).toContain(
      `@media (min-width: ${NAV_ROW_BELOW}px) and (max-width: ${NAV_COMPACT_BELOW - 1}px)`,
    );
    expect(css).toContain(`@media (max-width: ${NAV_ROW_BELOW - 1}px)`);
  });

  test("pages follow the live and pool offers, in navigation order", () => {
    expect(navPages({ live: false, pool: true }).map((p) => p.href)).toEqual([
      "/automatic",
      "/marvel_workstation",
      "/robot-studio",
      "/explore",
      "/workbench",
      "/pool",
      "/guide",
      "/report",
    ]);
    expect(navPages({ live: true, pool: false }).map((p) => p.href)).toEqual([
      "/live",
      "/automatic",
      "/marvel_workstation",
      "/robot-studio",
      "/explore",
      "/workbench",
      "/guide",
      "/report",
    ]);
  });

  test("current page matches the page and pages below it", () => {
    expect(isCurrentPage("/pool", "/pool")).toBe(true);
    expect(isCurrentPage("/pool", "/pool/recipes")).toBe(true);
    expect(isCurrentPage("/pool", "/poolside")).toBe(false);
    expect(isCurrentPage("/", "/explore")).toBe(false);
    expect(isCurrentPage("/", "/")).toBe(true);
    expect(isCurrentPage("/report", null)).toBe(false);
  });

  const make = (log: string[] = []) =>
    buildCommands({
      pages: navPages({ live: false, pool: true }),
      theme: "dark",
      language: "zh",
      setTheme: (value) => log.push(`theme:${value}`),
      setLanguage: (value) => log.push(`language:${value}`),
      toggleAgent: () => log.push("agent"),
      openConnections: () => log.push("connections"),
      openShortcuts: () => log.push("shortcuts"),
    });

  test("commands: pages, panels, theme and language with the current marked", () => {
    const log: string[] = [];
    const commands = make(log);
    expect(commands[0]).toMatchObject({ id: "go-home", href: "/" });
    expect(commands.find((c) => c.id === "go-pool")?.href).toBe("/pool");
    expect(commands.find((c) => c.id === "theme-dark")?.current).toBe(true);
    expect(commands.find((c) => c.id === "theme-light")?.current).toBe(false);
    expect(commands.find((c) => c.id === "language-zh")?.current).toBe(true);
    commands.find((c) => c.id === "theme-light")!.run!();
    commands.find((c) => c.id === "agent")!.run!();
    expect(log).toEqual(["theme:light", "agent"]);
  });

  test("filter: every word must match; label matches first; Chinese keywords", () => {
    const commands = make();
    expect(filterCommands(commands, "")).toEqual(commands);
    expect(filterCommands(commands, "pool")[0].id).toBe("go-pool");
    expect(filterCommands(commands, "fr3")[0].id).toBe("go-robot-studio");
    expect(filterCommands(commands, "拖拽")[0].id).toBe("go-robot-studio");
    expect(filterCommands(commands, "训练池")[0].id).toBe("go-pool");
    expect(filterCommands(commands, "theme dark").map((c) => c.id)).toEqual([
      "theme-dark",
    ]);
    expect(filterCommands(commands, "nothing like this")).toEqual([]);
    // The translated label is searched too.
    const zh = (text: string) => (text === "Report" ? "报告" : text);
    expect(filterCommands(commands, "报告", zh)[0].id).toBe("go-report");
  });
  test("filter: the marvel_workstation page is found by what its cards do, and by its name", () => {
    const commands = make();
    for (const words of [
      "deploy",
      "policy server",
      "demo client",
      "部署",
      "策略服务器",
      "评测客户端",
      "marvel_workstation",
      "workstation",
      "工作站",
    ])
      expect(
        filterCommands(commands, words).map((c) => c.id),
        words,
      ).toContain("go-marvel_workstation");
  });
});

describe("jobs", () => {
  test("counts only jobs that are still working", () => {
    expect(
      countPoolJobs({
        jobs: [
          { status: "running" },
          { status: "stalled" },
          { status: "cancelling" },
          { status: "planned" },
          { status: "done" },
          { status: "interrupted" },
        ],
      }),
    ).toBe(3);
    expect(
      countConversionJobs([
        { status: "queued" },
        { status: "running" },
        { status: "planned" },
        { status: "done" },
        { status: "failed" },
      ]),
    ).toBe(2);
  });

  test("odd answers count as none", () => {
    expect(countPoolJobs(null)).toBe(0);
    expect(countPoolJobs({ jobs: "x" })).toBe(0);
    expect(countConversionJobs({ detail: "Not found" })).toBe(0);
    expect(countConversionJobs([null, { status: 3 }])).toBe(0);
    expect(totalJobs(null)).toBe(0);
    expect(totalJobs({ pool: 1, conversion: 2 })).toBe(3);
  });
});

describe("theme boot script", () => {
  test("uses the same storage key and default as the theme preference", () => {
    expect(THEME_BOOT_KEY).toBe(THEME_STORAGE_KEY);
    expect(THEME_BOOT_DEFAULT).toBe(THEME_DEFAULT_PREFERENCE);
  });

  const boot = (storage: { getItem: () => string | null }) => {
    const attributes: Record<string, string> = {};
    new Function("document", "localStorage", THEME_BOOT_SCRIPT)(
      {
        documentElement: {
          setAttribute: (name: string, value: string) => {
            attributes[name] = value;
          },
        },
      },
      storage,
    );
    return attributes["data-theme"] ?? null;
  };

  test("no stored value follows the system: data-theme stays off", () => {
    expect(boot({ getItem: () => null })).toBeNull();
    expect(boot({ getItem: () => "purple" })).toBeNull();
  });

  test("an explicit choice wins; system leaves data-theme off", () => {
    expect(boot({ getItem: () => "light" })).toBe("light");
    expect(boot({ getItem: () => "dark" })).toBe("dark");
    expect(boot({ getItem: () => "system" })).toBeNull();
  });

  test("blocked storage applies the default and does not throw", () => {
    expect(
      boot({
        getItem: () => {
          throw new Error("SecurityError");
        },
      }),
    ).toBeNull();
  });
});

describe("shell.css", () => {
  test("uses tokens, never colour literals", () => {
    const css = readFileSync(
      join(import.meta.dir, "../../../styles/shell.css"),
      "utf8",
    ).replace(/\/\*[\s\S]*?\*\//g, "");
    expect(
      css.match(/#[0-9a-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(/gi),
    ).toBeNull();
  });
});
