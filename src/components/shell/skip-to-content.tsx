"use client";
import { useLocale } from "@/components/levi-locale";

/**
 * The first tab stop of every page: past the top bar and its menus to the
 * page's `main`. Visible only while focused (`.levi-skip`). The episode
 * viewer keeps its own links to its content and inspector after this one.
 */
export function SkipToContent() {
  const { t } = useLocale();
  return (
    <a
      className="levi-skip"
      href="#levi-main"
      onClick={(event) => {
        // The page's main area: a `main` (or role=main), else the viewer's.
        const main =
          document.querySelector<HTMLElement>("main, [role=main]") ??
          document.getElementById("vw-main");
        if (!main) return;
        event.preventDefault();
        if (!main.hasAttribute("tabindex")) main.tabIndex = -1;
        main.focus();
        // main's scroll-margin-top keeps it below the sticky bar.
        main.scrollIntoView?.({ block: "start" });
      }}
    >
      {t("Skip to main content")}
    </a>
  );
}
