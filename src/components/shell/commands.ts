/**
 * Commands of the command palette: page jumps and frame actions only
 * (theme, language, panels). Pure data, so the list and its filter are
 * tested without a DOM.
 */
import type { ThemePreference } from "@/lib/design/theme";

export type CommandGroup =
  | "Go to"
  | "Datasets"
  | "Panels"
  | "Appearance"
  | "Language";

export type Command = {
  id: string;
  group: CommandGroup;
  /** English label (the catalog key); shown translated. */
  label: string;
  /** Extra words a search matches (English and Chinese). */
  keywords?: string[];
  href?: string;
  run?: () => void;
  /** Marks the current choice (theme, language). */
  current?: boolean;
};

/** Pages of the navigation, in its order. */
export type NavPage = {
  href: string;
  label: string;
  /** A word after the label that says what the page is ("MARVEL0 robot"),
   * a catalogue key; the header drops it where the bar is short of room. */
  hint?: string;
  keywords: string[];
  /** `false`: in the palette and the go-to chords, not a header link. */
  header?: boolean;
  /**
   * The header link moves into the "More" menu when the bar runs short of
   * room: 1 = first (reference pages), 2 = next (see `navCollapseLevel`).
   * Without it the link stays. The palette and the go-to chords are
   * unaffected.
   */
  collapse?: 1 | 2;
};

export function navPages(options: { live: boolean; pool: boolean }): NavPage[] {
  const pages: NavPage[] = [];
  if (options.live)
    pages.push({
      href: "/live",
      label: "Live evaluation",
      keywords: ["live", "rollout", "实时评测"],
    });
  pages.push(
    {
      href: "/automatic",
      label: "liveService.navAutomatic",
      keywords: ["automatic", "aeri", "evaluation", "自动测评", "自动评测"],
    },
    {
      href: "/marvel0",
      label: "marvel0",
      hint: "marvel0.nav.hint",
      keywords: [
        "robot",
        "marvel0",
        "status",
        "deploy",
        "policy server",
        "demo client",
        "marvel0",
        "状态",
        "部署",
        "策略服务器",
        "评测客户端",
      ],
    },
    {
      href: "/explore",
      label: "Explore",
      keywords: ["datasets", "browse", "探索", "数据集"],
    },
    {
      href: "/workbench",
      label: "Conversion & review",
      keywords: ["convert", "review", "转换", "审核"],
      collapse: 2,
    },
  );
  if (options.pool)
    pages.push({
      href: "/pool",
      label: "Training pool",
      keywords: ["export", "recipe", "训练池", "导出"],
      collapse: 2,
    });
  pages.push(
    {
      href: "/guide",
      label: "Guide",
      keywords: ["help", "docs", "指南", "帮助"],
      collapse: 1,
    },
    {
      href: "/report",
      label: "Report",
      keywords: ["results", "报告"],
      collapse: 1,
    },
  );
  return pages;
}

/** The pages the header shows as links (the palette offers all of them). */
export function headerPages(pages: NavPage[]): NavPage[] {
  return pages.filter((page) => page.header !== false);
}

/**
 * Widths (CSS px) below which the header folds links into "More": the
 * reference pages (Guide, Report) first, then Conversion & review and the
 * training pool. Below `NAV_ROW_BELOW` the bar is two rows and the links
 * have a row of their own, so nothing folds. The same numbers are in
 * src/styles/shell.css (a test keeps them equal).
 */
export const NAV_COLLAPSE_WIDTHS = { first: 1560, second: 1500 } as const;
export const NAV_ROW_BELOW = 1000;

/** How many folding steps apply at a window width: 0 = every link shows. */
export function navCollapseLevel(width: number): 0 | 1 | 2 {
  if (width < NAV_ROW_BELOW) return 0;
  if (width < NAV_COLLAPSE_WIDTHS.second) return 2;
  if (width < NAV_COLLAPSE_WIDTHS.first) return 1;
  return 0;
}

/** The header links split at a folding level: `links` stay in the bar, `more`
 * go into the "More" menu (in navigation order). */
export function splitHeaderPages(
  pages: NavPage[],
  level: 0 | 1 | 2,
): { links: NavPage[]; more: NavPage[] } {
  const links: NavPage[] = [];
  const more: NavPage[] = [];
  for (const page of headerPages(pages))
    (page.collapse !== undefined && page.collapse <= level ? more : links).push(
      page,
    );
  return { links, more };
}

/** Whether `href` is the page at `pathname` (or a page below it). */
export function isCurrentPage(href: string, pathname: string | null): boolean {
  if (!pathname) return false;
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** A dataset the palette can open: `repo` is the address (`org/name`). */
export type PaletteDataset = { repo: string; name: string };

/**
 * The datasets of the catalogue answer (`GET /api/levi/catalog`): the local
 * ones (registered captures and datasets) and the public demos. Anything that
 * is not in the expected shape is skipped.
 */
export function paletteDatasets(body: unknown): PaletteDataset[] {
  if (!body || typeof body !== "object") return [];
  const { local, demos } = body as { local?: unknown; demos?: unknown };
  const found: PaletteDataset[] = [];
  if (Array.isArray(local))
    for (const entry of local) {
      const id = entry && typeof entry.id === "string" ? entry.id : null;
      if (!id || !/^[\w.-]+\/[\w.-]+$/.test(id)) continue;
      found.push({
        repo: id,
        name: typeof entry.name === "string" && entry.name ? entry.name : id,
      });
    }
  if (Array.isArray(demos))
    for (const id of demos)
      if (typeof id === "string" && /^[\w.-]+\/[\w.-]+$/.test(id))
        found.push({ repo: id, name: id });
  return found;
}

export function buildCommands(options: {
  pages: NavPage[];
  datasets?: PaletteDataset[];
  theme: ThemePreference;
  language: "en" | "zh";
  setTheme: (theme: ThemePreference) => void;
  setLanguage: (language: "en" | "zh") => void;
  toggleAgent: () => void;
  openConnections: () => void;
  openShortcuts: () => void;
}): Command[] {
  const go: Command[] = [
    {
      id: "go-home",
      group: "Go to",
      label: "Home",
      keywords: ["start", "首页"],
      href: "/",
    },
    ...options.pages.map((page) => ({
      id: `go-${page.href.slice(1)}`,
      group: "Go to" as const,
      label: page.label,
      keywords: page.keywords,
      href: page.href,
    })),
  ];
  const datasets: Command[] = (options.datasets ?? []).map((dataset) => ({
    id: `dataset-${dataset.repo}`,
    group: "Datasets" as const,
    label: dataset.name,
    keywords: [dataset.repo, "dataset", "数据集"],
    href: `/${dataset.repo}`,
  }));
  const panels: Command[] = [
    {
      id: "agent",
      group: "Panels",
      label: "Agent Workbench",
      keywords: ["agent", "model", "工作台"],
      run: options.toggleAgent,
    },
    {
      id: "connections",
      group: "Panels",
      label: "Accounts & connections",
      keywords: ["account", "connection", "账号", "连接"],
      run: options.openConnections,
    },
    {
      id: "shortcuts",
      group: "Panels",
      label: "Keyboard shortcuts",
      keywords: ["keys", "help", "快捷键"],
      run: options.openShortcuts,
    },
  ];
  const themes: Array<[ThemePreference, string, string[]]> = [
    ["system", "Theme: System", ["appearance", "auto", "外观", "系统"]],
    ["light", "Theme: Light", ["appearance", "外观", "浅色"]],
    ["dark", "Theme: Dark", ["appearance", "外观", "深色"]],
  ];
  const appearance: Command[] = themes.map(([value, label, keywords]) => ({
    id: `theme-${value}`,
    group: "Appearance",
    label,
    keywords,
    current: options.theme === value,
    run: () => options.setTheme(value),
  }));
  const languages: Command[] = [
    {
      id: "language-en",
      group: "Language",
      label: "English",
      keywords: ["language", "语言", "英文"],
      current: options.language === "en",
      run: () => options.setLanguage("en"),
    },
    {
      id: "language-zh",
      group: "Language",
      label: "中文",
      keywords: ["language", "chinese", "语言"],
      current: options.language === "zh",
      run: () => options.setLanguage("zh"),
    },
  ];
  return [...go, ...datasets, ...panels, ...appearance, ...languages];
}

function normalise(text: string): string {
  return text.toLocaleLowerCase().replace(/\s+/g, " ").trim();
}

/**
 * Commands whose translated label, English label or keywords contain every
 * word of the query, label matches first; an empty query keeps all, in order.
 */
export function filterCommands(
  commands: Command[],
  query: string,
  translate: (text: string) => string = (text) => text,
): Command[] {
  const words = normalise(query).split(" ").filter(Boolean);
  if (words.length === 0) return commands;
  const scored: Array<{ command: Command; score: number; index: number }> = [];
  commands.forEach((command, index) => {
    const label = normalise(`${translate(command.label)} ${command.label}`);
    const haystack = normalise(
      `${label} ${(command.keywords ?? []).join(" ")} ${translate(command.group)}`,
    );
    if (!words.every((word) => haystack.includes(word))) return;
    const score = words.every((word) => label.includes(word))
      ? label.startsWith(words[0])
        ? 0
        : 1
      : 2;
    scored.push({ command, score, index });
  });
  return scored
    .sort((a, b) => a.score - b.score || a.index - b.index)
    .map((entry) => entry.command);
}
