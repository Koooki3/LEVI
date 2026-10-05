"use client";
import { CircleAlert } from "lucide-react";
import { Icon, StatusDot } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { clock } from "./live-logic";
import type { DatasetRow, LiveSession, ServiceStatus } from "./types";

export type LiveSummary = {
  running: number;
  sessions: number;
  labelled: number;
  finished: number;
  lastError: string;
  /** Epoch seconds of that error, when known. */
  lastErrorAt: number | null;
};

type TimedError = { text: string; at: number | null; source: string };

/** Every error the status knows of, with its time when it has one: the
 * service's error events, each dataset's last error (at its last processing)
 * and the service's own last error (at the time of the event that says it,
 * if any). */
export function knownErrors(
  rows: Record<string, DatasetRow>,
  service: Pick<ServiceStatus, "last_error" | "events"> | null | undefined,
): TimedError[] {
  const out: TimedError[] = [];
  const events = (service?.events ?? []).filter((e) => e.level === "error");
  for (const e of events)
    out.push({ text: e.text, at: e.time, source: "event" });
  for (const [name, row] of Object.entries(rows))
    if (row.last_error)
      out.push({
        text: row.last_error,
        at: row.last_processed_at ?? null,
        source: name,
      });
  if (service?.last_error) {
    const said = events.filter((e) => e.text.includes(service.last_error!));
    out.push({
      text: service.last_error,
      at: said.length ? Math.max(...said.map((e) => e.time)) : null,
      source: "service",
    });
  }
  return out;
}

/** The newest of them: the one with the latest time; errors without a time
 * only when none has one. */
export function latestError(errors: TimedError[]): TimedError | null {
  let best: TimedError | null = null;
  for (const e of errors) {
    if (!best) best = e;
    else if (e.at !== null && (best.at === null || e.at > best.at)) best = e;
  }
  return best;
}

/** The page's one-line summary (proposal §8.5): running sessions, episodes
 * labelled out of those finished (mirrored), and the most recent error. */
export function liveSummary(
  sessions: LiveSession[],
  rows: Record<string, DatasetRow>,
  service: Pick<ServiceStatus, "last_error" | "events"> | null | undefined,
): LiveSummary {
  const list = Object.values(rows);
  const latest = latestError(knownErrors(rows, service));
  return {
    running: sessions.filter((s) => s.state === "running").length,
    sessions: sessions.length,
    labelled: list.reduce((n, r) => n + (r.done ?? 0), 0),
    finished: list.reduce((n, r) => n + (r.episodes ?? 0), 0),
    lastError: latest?.text ?? "",
    lastErrorAt: latest?.at ?? null,
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
              <span>
                {summary.lastError}
                {summary.lastErrorAt !== null && (
                  <span className="pg-live-summary__when">
                    {" · "}
                    {clock(summary.lastErrorAt)}
                  </span>
                )}
              </span>
            </span>
          ) : (
            t("none")
          )}
        </dd>
      </div>
    </dl>
  );
}
