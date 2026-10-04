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
import { useEffect, useMemo, useState } from "react";
import {
  Bot,
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
import { isCurrentPage, navPages } from "./shell/commands";
import { isApplePlatform, paletteKeys } from "./shell/global-keys";
import { JobsMenu } from "./shell/jobs-menu";
import { useShell } from "./shell/shell-context";
import {
  SHELL_EVENTS,
  openAgentConnections,
  toggleAgentWorkbench,
} from "./shell/shell-events";
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

export default function LeviHeader() {
  const { t, language, setLanguage } = useLocale();
  const pathname = usePathname();
  const { theme, setTheme, setPaletteOpen, setShortcutsOpen } = useShell();
  // The live workspace's own LEVI (`levi live start --ui`) offers no training
  // pool: the pool is the product LEVI's (docs/LIVE.md).
  const { enabled, embedded } = useLivePulse();
  const pool = offersTrainingPool(enabled, embedded);
  const pages = useMemo(
    () => navPages({ live: Boolean(enabled), pool }),
    [enabled, pool],
  );
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

  return (
    <>
      <header className="levi-shell-header">
        <Link href="/" className="levi-shell-brand ds-focus">
          <span className="levi-shell-mark" aria-hidden="true" />
          <span>LEVI</span>
        </Link>
        <nav className="levi-shell-nav" aria-label={t("Main navigation")}>
          {pages.map((page) =>
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
                aria-current={
                  isCurrentPage(page.href, pathname) ? "page" : undefined
                }
              >
                {t(page.label)}
              </Link>
            ),
          )}
        </nav>
        <div className="levi-shell-actions">
          <button
            type="button"
            className="levi-shell-search ds-focus"
            onClick={() => setPaletteOpen(true)}
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
