"use client";
// The event timeline of a run: state moves and notes, newest at the bottom.
// While the last episode is not labelled, events that would tell how it ended
// are shown as hidden.
import { useLocale } from "@/components/levi-locale";
import { eventKind, eventReason, maskEvent } from "./run-logic";
import { stateName } from "./run-parts";
import type { RunEvent } from "./types";

export function RunTimeline({
  events,
  blind,
}: {
  events: RunEvent[];
  blind: boolean;
}) {
  const { t, language } = useLocale();
  const locale = language === "zh" ? "zh-CN" : "en";
  if (events.length === 0)
    return <p className="ar-muted">{t("automatic.run.timeline.empty")}</p>;
  return (
    <ol className="ar-timeline" aria-label={t("automatic.run.timeline.title")}>
      {events.map((raw) => {
        const event = maskEvent(raw, blind);
        const hidden = event.kind === "hidden";
        return (
          <li key={event.seq}>
            <span className="ar-muted">#{event.seq}</span>
            <time dateTime={new Date(event.at).toISOString()}>
              {new Date(event.at).toLocaleTimeString(locale)}
            </time>
            {hidden ? (
              <span>{t("automatic.run.timeline.hidden")}</span>
            ) : (
              <>
                <span>{eventKind(event.kind, t)}</span>
                {event.from && event.to && (
                  <span>
                    {stateName(event.from, t)} → {stateName(event.to, t)}
                  </span>
                )}
                {event.reason && <span>{eventReason(event.reason, t)}</span>}
              </>
            )}
          </li>
        );
      })}
    </ol>
  );
}
