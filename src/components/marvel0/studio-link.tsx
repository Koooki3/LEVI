"use client";
// A way from the marvel0 to the offline FR3 3D preview (/robot-studio). It
// opens a page that has a model and a solver in the browser and no
// connection to a robot, so the card does not depend on the status polls and
// is there even while they fail.
import Link from "next/link";
import { ArrowRight, Box } from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

export function StudioLink() {
  const { t } = useLocale();
  return (
    <aside className="rg-studio" data-testid="marvel0-studio-link">
      <Icon icon={Box} className="rg-studio__icon" />
      <div className="rg-studio__text">
        <h2 id="marvel0-studio-title">{t("marvel0.studio.title")}</h2>
        <p className="rg-studio__note">{t("marvel0.studio.note")}</p>
      </div>
      <Link
        href="/robot-studio"
        className="ds-btn ds-btn--secondary ds-btn--md ds-focus rg-studio__open"
        aria-describedby="marvel0-studio-title"
      >
        <span className="ds-btn__label">{t("marvel0.studio.open")}</span>
        <Icon icon={ArrowRight} />
      </Link>
    </aside>
  );
}
