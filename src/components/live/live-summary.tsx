"use client";
import { CircleAlert } from "lucide-react";
import { Icon, StatusDot, type Tone as DsTone } from "@/components/ds";
import { sessionInProgress, sessionWaiting } from "./live-filters";
import { useLocale } from "@/components/levi-locale";
import { clock } from "./live-logic";
import type { DatasetRow, LiveSession, ServiceStatus } from "./types";

export type LiveSummary = {
  /** Sessions in progress (running an episode or returning home). */
  inProgress: number;
  /** Sessions waiting between episodes. */
  waiting: number;
  sessions: number;
  /** Episodes the background service labelled (`done`). */
  labelled: number;
  /** Episodes mirrored into the live workspace (`episodes`). */
  mirrored: number;
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

/** The page's one-line summary (proposal §8.5): sessions in progress and
 * waiting (the same predicates as the status filter and the delete guard),
 * episodes labelled out of those mirrored, and the most recent error. */
export function liveSummary(
  sessions: LiveSession[],
  rows: Record<string, DatasetRow>,
  service: Pick<ServiceStatus, "last_error" | "events"> | null | undefined,
): LiveSummary {
  const list = Object.values(rows);
  const latest = latestError(knownErrors(rows, service));
  return {
    inProgress: sessions.filter(sessionInProgress).length,
    waiting: sessions.filter(sessionWaiting).length,
    sessions: sessions.length,
    labelled: list.reduce((n, r) => n + (r.done ?? 0), 0),
    mirrored: list.reduce((n, r) => n + (r.episodes ?? 0), 0),
    lastError: latest?.text ?? "",
    lastErrorAt: latest?.at ?? null,
  };
}

const FR3_SUMMARY: Record<string, [string, DsTone]> = {
  ok: ["OK", "success"],
  red: ["Red light", "danger"],
  offline: ["Monitor offline", "warning"],
  missing: ["Monitor not running", "warning"],
};

/** The summary row. `fr3` is the arm's health state, so the first screen
 * says whether the robot's own signal is there; `stale` (the service or the
 * core is not answering) stops the breathing dot and says the figures are the
 * last known ones. `fr3` undefined: the state is unknown. */
export function LiveSummaryBar({
  summary,
  fr3,
  stale = false,
  sessionsStale = false,
  fr3LastKnown = false,
}: {
  summary: LiveSummary;
  fr3?: string | null;
  /** The service's figures (labelled / mirrored) are the last known ones. */
  stale?: boolean;
  /** The session list itself is not current (core down, or `/sessions`
   * failing): the dot stops breathing. A stopped service alone does not
   * make the sessions stale: they are read from the client's files. */
  sessionsStale?: boolean;
  /** The arm's state shown is the last one read (a red light stays red). */
  fr3LastKnown?: boolean;
}) {
  const { t } = useLocale();
  const [fr3Label, fr3Tone] = fr3
    ? (FR3_SUMMARY[fr3] ?? [fr3, "neutral" as DsTone])
    : ["Unknown", "neutral" as DsTone];
  return (
    <dl className="pg-live-summary" aria-label={t("Summary")}>
      <div>
        <dt>{t("Sessions in progress")}</dt>
        <dd>
          <StatusDot
            tone={summary.inProgress && !sessionsStale ? "success" : "neutral"}
            live={summary.inProgress > 0 && !sessionsStale}
          >
            <span className="tabular">
              {summary.inProgress} / {summary.sessions}
            </span>
            {summary.waiting > 0 && (
              <span className="pg-pool-muted">
                {" · "}
                {t("waiting")} {summary.waiting}
              </span>
            )}
          </StatusDot>
        </dd>
      </div>
      <div
        data-tone={
          fr3Tone === "danger" || fr3Tone === "warning" ? fr3Tone : undefined
        }
      >
        <dt>{t("FR3 robot arm")}</dt>
        <dd>
          <StatusDot tone={fr3Tone}>{t(fr3Label)}</StatusDot>
          {fr3LastKnown && (
            <span className="pg-pool-muted"> · {t("last known")}</span>
          )}
        </dd>
      </div>
      <div>
        <dt>{t("Labelled / mirrored episodes")}</dt>
        <dd className="tabular">
          {summary.labelled.toLocaleString()} /{" "}
          {summary.mirrored.toLocaleString()}
          {stale && <span className="pg-pool-muted"> · {t("last known")}</span>}
        </dd>
      </div>
      <div className="pg-live-summary__error">
        <dt>{t("Last error")}</dt>
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
