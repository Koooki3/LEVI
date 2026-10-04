import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import {
  THEME_DEFAULT_PREFERENCE,
  THEME_STORAGE_KEY,
} from "@/lib/design/theme";
import {
  buildCommands,
  filterCommands,
  isCurrentPage,
  navPages,
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
  test("pages follow the live and pool offers, in navigation order", () => {
    expect(navPages({ live: false, pool: true }).map((p) => p.href)).toEqual([
      "/explore",
      "/workbench",
      "/pool",
      "/guide",
      "/report",
    ]);
    expect(navPages({ live: true, pool: false }).map((p) => p.href)).toEqual([
      "/live",
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
    expect(filterCommands(commands, "训练池")[0].id).toBe("go-pool");
    expect(filterCommands(commands, "theme dark").map((c) => c.id)).toEqual([
      "theme-dark",
    ]);
    expect(filterCommands(commands, "nothing like this")).toEqual([]);
    // The translated label is searched too.
    const zh = (text: string) => (text === "Report" ? "报告" : text);
    expect(filterCommands(commands, "报告", zh)[0].id).toBe("go-report");
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

  test("no stored value means dark during the transition (until stage 5)", () => {
    expect(boot({ getItem: () => null })).toBe("dark");
    expect(boot({ getItem: () => "purple" })).toBe("dark");
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
    ).toBe("dark");
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
