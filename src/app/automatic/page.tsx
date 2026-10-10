"use client";
// Automatic evaluation: recent runs and the launch panel. A run is opened in
// its own window (/automatic/runs/<id>). The first release runs dry runs
// only; nothing here moves a robot.
import "@/components/pages-ui/pages.css";
import "@/components/automatic/run-styles.css";
import Link from "next/link";
import { GitCompareArrows, WandSparkles } from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { LaunchPanel } from "@/components/automatic/launch-panel";
import { RunList } from "@/components/automatic/run-list";

export default function AutomaticPage() {
  const { t } = useLocale();
  return (
    <main className="ds-root pg-workbench ar-page">
      <div className="ar-head">
        <h1>{t("automatic.run.page.title")}</h1>
        <Link
          href="/automatic/new"
          className="ds-btn ds-btn--secondary ds-btn--md ds-focus ar-link"
        >
          <Icon icon={WandSparkles} />
          {t("automatic.run.page.wizard")}
        </Link>
        <Link
          href="/automatic/campaigns"
          className="ds-btn ds-btn--secondary ds-btn--md ds-focus ar-link"
        >
          <Icon icon={GitCompareArrows} />
          {t("automatic.campaign.list.title")}
        </Link>
      </div>
      <p>{t("automatic.run.page.intro")}</p>
      <RunList />
      <LaunchPanel />
    </main>
  );
}
