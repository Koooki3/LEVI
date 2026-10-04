"use client";
/** The keyboard shortcut list ("?"): every shortcut, grouped by where. */
import { Fragment, useMemo } from "react";
import { Dialog, Kbd } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { globalShortcut, isApplePlatform, shortcutGroups } from "./global-keys";
import { SHELL_OVERLAY_CLASS, useShell } from "./shell-context";

export function ShortcutsDialog() {
  const { t } = useLocale();
  const { shortcutsOpen, setShortcutsOpen } = useShell();
  const groups = useMemo(
    () => (shortcutsOpen ? shortcutGroups(isApplePlatform()) : []),
    [shortcutsOpen],
  );
  return (
    <Dialog
      open={shortcutsOpen}
      onClose={() => setShortcutsOpen(false)}
      title={t("Keyboard shortcuts")}
      description={t(
        "Shortcuts do not fire while you type in a field or compose text with an input method.",
      )}
      size="md"
      // ⌘K / Ctrl+K still reaches the frame, so it toggles the palette here.
      passKeys={(event) => globalShortcut(event.nativeEvent) === "palette"}
      className={`${SHELL_OVERLAY_CLASS} levi-shortcuts`}
    >
      {groups.map((group) => (
        <section key={group.title} className="levi-shortcuts__group">
          <h3 className="levi-shortcuts__title">{t(group.title)}</h3>
          <dl className="levi-shortcuts__list">
            {group.rows.map((row) => (
              <div key={row.label} className="levi-shortcuts__row">
                <dt>{t(row.label)}</dt>
                <dd>
                  {row.keys.map((combo, index) => (
                    <Fragment key={combo.join("+")}>
                      {index > 0 && (
                        <span className="levi-shortcuts__or">{t("or")}</span>
                      )}
                      <span className="levi-shortcuts__combo">
                        {combo.map((key) => (
                          <Kbd key={key}>{key}</Kbd>
                        ))}
                      </span>
                    </Fragment>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </Dialog>
  );
}
