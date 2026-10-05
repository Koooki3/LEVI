"use client";
import { useLocale } from "@/components/levi-locale";

/**
 * "Skip to" links, the first tab stops of the episode viewer: past the tab
 * bar and the episode list, to the content, or to the inspector (shown when
 * there is one). They are visible only while focused.
 */
export function SkipLinks({ inspector }: { inspector: boolean }) {
  const { t } = useLocale();
  const go = (id: string) => (event: React.MouseEvent) => {
    const target = document.getElementById(id);
    if (!target) return;
    event.preventDefault();
    target.focus();
    target.scrollIntoView?.({ block: "nearest" });
  };
  return (
    <nav className="vw-skip" aria-label={t("Skip links")}>
      <a href="#vw-main" onClick={go("vw-main")}>
        {t("Skip to content")}
      </a>
      {inspector && (
        <a href="#vw-inspector-heading" onClick={go("vw-inspector-heading")}>
          {t("Skip to inspector")}
        </a>
      )}
    </nav>
  );
}
