"use client";
// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
/**
 * The global top bar (design stage 2): wordmark, the page navigation, and on
 * the right the command palette, Jobs, the Agent Workbench drawer, settings,
 * theme and language. Pages and routes are the same as before; only the
 * frame changed. Styles: src/styles/shell.css (tokens only).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  Activity,
  Bot,
  Ellipsis,
  Keyboard,
  Languages,
  Monitor,
  Moon,
  Search,
  Settings,
  Sun,
  UserRoundCog,
} from "lucide-react";
import { Button, Icon, IconButton, Kbd, Menu, Tooltip } from "@/components/ds";
import type { ThemePreference } from "@/lib/design/theme";
import { useLocale } from "./levi-locale";
import { offersTrainingPool } from "./live/embedding";
import { LiveNavLink } from "./live/live-nav";
import { useLivePulse } from "./live/use-live-pulse";
import { CommandPalette } from "./shell/command-palette";
import {
  isCurrentPage,
  NAV_ROW_BELOW,
  navCollapseLevel,
  navPages,
  splitHeaderPages,
} from "./shell/commands";
import { isApplePlatform, paletteKeys } from "./shell/global-keys";
import { navScrollTarget } from "./shell/nav-scroll";
import { JobsMenu } from "./shell/jobs-menu";
import { useShell } from "./shell/shell-context";
import {
  SHELL_EVENTS,
  openAgentConnections,
  requestGo,
  toggleAgentWorkbench,
} from "./shell/shell-events";
import { LeviWordmark } from "./shell/brand";
import type { MotionPreference } from "./shell/motion-preference";
import { ShortcutsDialog } from "./shell/shortcuts-dialog";

const THEME_ICON = { system: Monitor, light: Sun, dark: Moon } as const;
const THEME_LABEL = { system: "System", light: "Light", dark: "Dark" } as const;

/** Whether the Agent Workbench drawer is open (it announces changes). */
function useAgentOpen(): boolean {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const onState = (event: Event) =>
      setOpen(Boolean((event as CustomEvent<{ open?: boolean }>).detail?.open));
    window.addEventListener(SHELL_EVENTS.agentState, onState);
    return () => window.removeEventListener(SHELL_EVENTS.agentState, onState);
  }, []);
  return open;
}

/** A value read from the window's width, updated on resize. `server` is what
 * the server and the first client render say. */
function useWidthValue<T>(read: (width: number) => T, server: T): T {
  return useSyncExternalStore(
    (notify) => {
      window.addEventListener("resize", notify);
      return () => window.removeEventListener("resize", notify);
    },
    () => read(window.innerWidth),
    () => server,
  );
}

/**
 * How many folding steps of the navigation apply at the window's width (see
 * `navCollapseLevel`). The server and the first client render say 0 (every
 * link); styles/shell.css hides the folded links by width until this catches
 * up, so the bar does not jump.
 */
function useNavCollapseLevel(): 0 | 1 | 2 | 3 {
  return useWidthValue(navCollapseLevel, 0);
}

/** Whether the links have a row of their own (below `NAV_ROW_BELOW`): there
 * "More" is a button with an icon only, so the first links keep their room
 * (the first three show whole at 420 px). */
function useTwoRowBar(): boolean {
  return useWidthValue((width) => width < NAV_ROW_BELOW, false);
}

export default function LeviHeader() {
  const { t, language, setLanguage } = useLocale();
  const pathname = usePathname();
  const {
    theme,
    setTheme,
    motion,
    setMotion,
    setPaletteOpen,
    setShortcutsOpen,
  } = useShell();
  // The live workspace's own LEVI (`levi live start --ui`) offers no training
  // pool: the pool is the product LEVI's (docs/LIVE.md).
  const { enabled, embedded } = useLivePulse();
  const pool = offersTrainingPool(enabled, embedded);
  const pages = useMemo(
    // The Live evaluation entry is always offered: with no live service it
    // opens a page that says how to start one (LiveNavLink draws it plain).
    () => navPages({ live: true, pool }),
    [pool],
  );
  const level = useNavCollapseLevel();
  const twoRows = useTwoRowBar();
  const { links, more } = useMemo(
    () => splitHeaderPages(pages, level),
    [pages, level],
  );
  // The menu is always in the bar: it holds the pages that are only menu items
  // and, at the widths where links fold, the folded ones. Before the width is
  // known (level 0) it holds only the first; the folded links are hidden by
  // the styles at those widths, and the menu cannot be opened yet.
  const moreCurrent = more.some((page) => isCurrentPage(page.href, pathname));
  // The bar scrolls when the links do not fit (the two-row bar of a narrow
  // window). The current page (marvel_workstation among them) is brought into view only
  // when it is not whole there already, and then to the start of a link: a bar
  // rolled for no reason cut the first link ("ion 1" for "Live evaluation 1").
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const nav = navRef.current;
    if (!nav) return;
    const roll = () => {
      if (nav.scrollWidth <= nav.clientWidth) return;
      const links = Array.from(nav.children) as HTMLElement[];
      const current = links.findIndex(
        (el) =>
          el.matches('[aria-current="page"]') ||
          el.querySelector('[aria-current="page"]'),
      );
      if (current < 0) return;
      const box = nav.getBoundingClientRect();
      const target = navScrollTarget({
        edges: links.map((el) => {
          const r = el.getBoundingClientRect();
          return [
            r.left - box.left + nav.scrollLeft,
            r.right - box.left + nav.scrollLeft,
          ] as const;
        }),
        current,
        scrollLeft: nav.scrollLeft,
        width: nav.clientWidth,
      });
      if (target !== null) nav.scrollLeft = target;
    };
    roll();
    // The links change width after the first paint (the live count arrives,
    // the font loads, the language changes): look again when they do.
    if (typeof ResizeObserver === "undefined") return;
    const watch = new ResizeObserver(roll);
    watch.observe(nav);
    for (const child of Array.from(nav.children)) watch.observe(child);
    return () => watch.disconnect();
  }, [pathname, level, language, twoRows]);
  const agentOpen = useAgentOpen();
  // Rendered after mount so the server and the first client render agree.
  const [apple, setApple] = useState(false);
  useEffect(() => setApple(isApplePlatform()), []);
  const keys = paletteKeys(apple);

  const switchLabel = t(
    language === "zh" ? "Switch to English" : "Switch to Chinese",
  );
  const themeLabel = `${t("Theme")}: ${t(THEME_LABEL[theme])}`;
  const themes: ThemePreference[] = ["system", "light", "dark"];
  const motions: Array<[MotionPreference, string]> = [
    ["system", "Motion: follow the system"],
    ["reduce", "Motion: reduced"],
    ["full", "Motion: normal"],
  ];

  return (
    <>
      <header className="levi-shell-header">
        <Link href="/" className="levi-shell-brand ds-focus">
          <LeviWordmark size={20} />
        </Link>
        <div className="levi-shell-navwrap">
          <nav
            ref={navRef}
            className="levi-shell-nav"
            aria-label={t("Main navigation")}
          >
            {links.map((page) =>
              page.href === "/live" ? (
                <LiveNavLink
                  key={page.href}
                  current={isCurrentPage(page.href, pathname)}
                />
              ) : (
                <Link
                  key={page.href}
                  href={page.href}
                  className="levi-shell-link ds-focus"
                  data-collapse={page.collapse}
                  aria-current={
                    isCurrentPage(page.href, pathname) ? "page" : undefined
                  }
                >
                  {t(page.barLabel ?? page.label)}
                </Link>
              ),
            )}
          </nav>
          {more.length > 0 && (
            <div
              className={"levi-shell-more" + (moreCurrent ? " is-current" : "")}
            >
              <Menu
                label={t("More")}
                variant="ghost"
                align="start"
                icon={twoRows ? Ellipsis : undefined}
                iconOnly={twoRows}
                triggerClassName="levi-shell-more__trigger"
                items={more.map((page) => ({
                  id: page.href,
                  label: t(page.label),
                  checked: isCurrentPage(page.href, pathname),
                  onSelect: () => requestGo(page.href),
                }))}
              />
            </div>
          )}
        </div>
        <div className="levi-shell-actions">
          <button
            type="button"
            className="levi-shell-search ds-focus"
            onClick={() => setPaletteOpen(true)}
            aria-label={t("Search")}
            aria-keyshortcuts={apple ? "Meta+K" : "Control+K"}
          >
            <Icon icon={Search} />
            <span className="levi-shell-search__label">{t("Search")}</span>
            <span className="levi-shell-search__keys" aria-hidden="true">
              {keys.map((key) => (
                <Kbd key={key}>{key}</Kbd>
              ))}
            </span>
          </button>
          <JobsMenu pool={pool} />
          <IconButton
            icon={Bot}
            label={t("Agent Workbench")}
            pressed={agentOpen}
            tooltipPlacement="bottom"
            onClick={toggleAgentWorkbench}
          />
          <Menu
            label={t("Settings")}
            icon={Settings}
            iconOnly
            variant="ghost"
            align="end"
            tooltip={t("Settings")}
            items={[
              {
                id: "connections",
                icon: UserRoundCog,
                label: t("Accounts & connections"),
                onSelect: openAgentConnections,
              },
              {
                id: "palette",
                icon: Search,
                label: t("Command palette"),
                shortcut: keys.join(" "),
                onSelect: () => setPaletteOpen(true),
              },
              {
                id: "shortcuts",
                icon: Keyboard,
                label: t("Keyboard shortcuts"),
                shortcut: "?",
                onSelect: () => setShortcutsOpen(true),
              },
              ...motions.map(([value, label]) => ({
                id: `motion-${value}`,
                icon: Activity,
                label: t(label),
                checked: motion === value,
                onSelect: () => setMotion(value),
              })),
            ]}
          />
          <Menu
            label={themeLabel}
            icon={THEME_ICON[theme]}
            iconOnly
            variant="ghost"
            align="end"
            tooltip={themeLabel}
            items={themes.map((value) => ({
              id: value,
              icon: THEME_ICON[value],
              label: t(THEME_LABEL[value]),
              checked: theme === value,
              onSelect: () => setTheme(value),
            }))}
          />
          <Tooltip content={switchLabel} placement="bottom">
            <Button
              variant="ghost"
              size="sm"
              icon={Languages}
              className="levi-shell-language"
              onClick={() => setLanguage(language === "zh" ? "en" : "zh")}
            >
              {language === "zh" ? (
                <span lang="en">EN</span>
              ) : (
                <span lang="zh">中文</span>
              )}
            </Button>
          </Tooltip>
        </div>
      </header>
      <CommandPalette pages={pages} />
      <ShortcutsDialog />
    </>
  );
}
