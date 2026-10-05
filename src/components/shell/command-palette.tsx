"use client";
/**
 * The command palette (⌘K / Ctrl+K): jump to a page or a dataset, open a
 * panel, change the theme or the language. A combobox over a list box: type to filter,
 * ↑/↓ move, Enter runs, Escape closes and focus returns to where it was.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, CornerDownLeft, Search } from "lucide-react";
import { Dialog, Icon, Kbd } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { leviApi } from "@/components/levi-api";
import {
  buildCommands,
  filterCommands,
  paletteDatasets,
  type Command,
  type NavPage,
  type PaletteDataset,
} from "./commands";
import { globalShortcut } from "./global-keys";
import { SHELL_OVERLAY_CLASS, useShell } from "./shell-context";
import { openAgentConnections, toggleAgentWorkbench } from "./shell-events";

export function CommandPalette({ pages }: { pages: NavPage[] }) {
  const { t, language, setLanguage } = useLocale();
  const router = useRouter();
  const { paletteOpen, setPaletteOpen, theme, setTheme, setShortcutsOpen } =
    useShell();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [datasets, setDatasets] = useState<PaletteDataset[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const listId = useId();

  const commands = useMemo(
    () =>
      buildCommands({
        pages,
        datasets,
        theme,
        language,
        setTheme,
        setLanguage,
        toggleAgent: toggleAgentWorkbench,
        openConnections: openAgentConnections,
        openShortcuts: () => setShortcutsOpen(true),
      }),
    [pages, datasets, theme, language, setTheme, setLanguage, setShortcutsOpen],
  );
  const results = useMemo(
    () => filterCommands(commands, query, t),
    // `t` changes with the language, which `commands` already follows.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [commands, query, language],
  );

  useEffect(() => {
    if (!paletteOpen) {
      setQuery("");
      setActive(0);
      return;
    }
    // The datasets are read when the palette opens (the catalogue is the
    // existing answer; no new route). Without an answer there are just none.
    let stale = false;
    leviApi<unknown>("catalog")
      .then((body) => {
        if (!stale) setDatasets(paletteDatasets(body));
      })
      .catch(() => undefined);
    return () => {
      stale = true;
    };
  }, [paletteOpen]);
  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    document
      .getElementById(`${listId}-${active}`)
      ?.scrollIntoView?.({ block: "nearest" });
  }, [active, listId]);

  const run = (command: Command | undefined) => {
    if (!command) return;
    setPaletteOpen(false);
    if (command.href) router.push(command.href);
    else command.run?.();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (results.length === 0) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      setActive((index) => (index + step + results.length) % results.length);
    } else if (event.key === "Enter") {
      event.preventDefault();
      run(results[active]);
    }
  };

  let lastGroup = "";
  return (
    <Dialog
      open={paletteOpen}
      onClose={() => setPaletteOpen(false)}
      title={t("Command palette")}
      initialFocus={input}
      size="md"
      // ⌘K / Ctrl+K still reaches the frame, so it toggles the palette here.
      passKeys={(event) => globalShortcut(event.nativeEvent) === "palette"}
      className={`${SHELL_OVERLAY_CLASS} levi-palette`}
    >
      <div className="levi-palette__search">
        <Icon icon={Search} />
        <input
          ref={input}
          type="text"
          className="levi-palette__input ds-focus"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={
            results.length ? `${listId}-${active}` : undefined
          }
          aria-label={t("Search commands, pages and datasets")}
          placeholder={t("Search commands, pages and datasets")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
          autoComplete="off"
          spellCheck={false}
        />
      </div>
      <div
        id={listId}
        role="listbox"
        aria-label={t("Commands")}
        className="levi-palette__list"
      >
        {results.map((command, index) => {
          const heading =
            command.group !== lastGroup ? (
              <div
                className="levi-palette__group"
                role="presentation"
                key={`${command.group}-heading`}
              >
                {t(command.group)}
              </div>
            ) : null;
          lastGroup = command.group;
          return [
            heading,
            <div
              key={command.id}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === active}
              data-current={command.current || undefined}
              className="levi-palette__option"
              onPointerMove={() => setActive(index)}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => run(command)}
            >
              <span className="levi-palette__label">{t(command.label)}</span>
              {command.current && (
                <span className="levi-palette__current">
                  <Icon icon={Check} label={t("Current")} />
                </span>
              )}
              {index === active && (
                <span className="levi-palette__enter" aria-hidden="true">
                  <Icon icon={CornerDownLeft} />
                </span>
              )}
            </div>,
          ];
        })}
        {results.length === 0 && (
          <p className="levi-palette__empty" role="status">
            {t("No matching commands")}
          </p>
        )}
      </div>
      <p className="levi-palette__hint">
        <Kbd>↑</Kbd>
        <Kbd>↓</Kbd> {t("to move")} · <Kbd>Enter</Kbd> {t("to open")} ·{" "}
        <Kbd>Esc</Kbd> {t("to close")}
      </p>
    </Dialog>
  );
}
