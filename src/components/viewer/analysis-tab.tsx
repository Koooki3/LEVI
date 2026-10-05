"use client";
import type { ReactNode } from "react";
import { Activity, ListFilter, Stethoscope } from "lucide-react";
import { SegmentedControl } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import type { AnalysisView } from "./viewer-tabs";

const VIEWS: Array<{
  value: AnalysisView;
  label: string;
  icon: typeof Activity;
  description: string;
}> = [
  {
    value: "insights",
    label: "Action Insights",
    icon: Activity,
    description:
      "How actions vary within this episode and across the dataset: autocorrelation, lag, speed and variance.",
  },
  {
    value: "filtering",
    label: "Filtering",
    icon: ListFilter,
    description:
      "Find episodes to flag by length, speed, jerk and other signals, then review them in the episode list.",
  },
  {
    value: "doctor",
    label: "Doctor",
    icon: Stethoscope,
    description:
      "Dataset quality diagnostics (powered by lerobot-doctor): structure, timestamps, actions and videos.",
  },
];

/**
 * The "Analysis" tab: one segmented control over action insights, filtering
 * and the dataset doctor (formerly three tabs). `children` renders the
 * chosen view, so each keeps its own loading and state.
 */
export function AnalysisTab({
  view,
  onViewChange,
  children,
}: {
  view: AnalysisView;
  onViewChange: (view: AnalysisView) => void;
  children: (view: AnalysisView) => ReactNode;
}) {
  const { t } = useLocale();
  const current = VIEWS.find((item) => item.value === view) ?? VIEWS[0];
  return (
    <section className="flex flex-col gap-4" aria-label={t("Analysis")}>
      <h2 className="ds-sr-only">{t(current.label)}</h2>
      <div className="vw-analysis-head">
        <SegmentedControl
          label={t("Analysis view")}
          value={current.value}
          onChange={(value) => onViewChange(value as AnalysisView)}
          options={VIEWS.map((item) => ({
            value: item.value,
            label: t(item.label),
            icon: item.icon,
          }))}
        />
        <p>{t(current.description)}</p>
      </div>
      <div
        className="vw-chart vw-analysis-body"
        data-analysis-view={current.value}
      >
        {children(current.value)}
      </div>
    </section>
  );
}
