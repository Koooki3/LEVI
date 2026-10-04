"use client";
import { CircleAlert } from "lucide-react";
import { Icon, StatusDot } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import type { DatasetRow, LiveSession } from "./types";

export type LiveSummary = {
  running: number;
  sessions: number;
  labelled: number;
  finished: number;
  lastError: string;
};

/** The page's one-line summary (proposal §8.5): running sessions, episodes
 * labelled out of those finished (mirrored), and the most recent error (the
 * service's own, else the first dataset that reports one). */
export function liveSummary(
  sessions: LiveSession[],
  rows: Record<string, DatasetRow>,
  serviceError: string | undefined,
): LiveSummary {
  const list = Object.values(rows);
  return {
    running: sessions.filter((s) => s.state === "running").length,
    sessions: sessions.length,
    labelled: list.reduce((n, r) => n + (r.done ?? 0), 0),
    finished: list.reduce((n, r) => n + (r.episodes ?? 0), 0),
    lastError: serviceError || list.find((r) => r.last_error)?.last_error || "",
  };
}

export function LiveSummaryBar({ summary }: { summary: LiveSummary }) {
  const { t } = useLocale();
  return (
    <dl className="pg-live-summary" aria-label={t("Summary")}>
      <div>
        <dt>{t("Running sessions")}</dt>
        <dd>
          <StatusDot
            tone={summary.running ? "success" : "neutral"}
            live={summary.running > 0}
          >
            <span className="tabular">
              {summary.running} / {summary.sessions}
            </span>
          </StatusDot>
        </dd>
      </div>
      <div>
        <dt>{t("Labelled / finished episodes")}</dt>
        <dd className="tabular">
          {summary.labelled.toLocaleString()} /{" "}
          {summary.finished.toLocaleString()}
        </dd>
      </div>
      <div className="pg-live-summary__error">
        <dt>{t("Most recent error")}</dt>
        <dd>
          {summary.lastError ? (
            <span className="pg-live-summary__msg">
              <Icon icon={CircleAlert} label={t("Error")} />
              <span>{summary.lastError}</span>
            </span>
          ) : (
            t("none")
          )}
        </dd>
      </div>
    </dl>
  );
}
