"use client";
// The event timeline of a run: state moves and notes, newest at the bottom.
// While the last episode is not labelled, events that would tell how it ended
// are shown as hidden.
import { CircleAlert } from "lucide-react";
import { useEffect, useRef } from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { eventKind, eventReason, maskEvent } from "./run-logic";
import { Note, stateName } from "./run-parts";
import type { RunEvent } from "./types";

export function RunTimeline({
  events,
  blind,
  loaded = true,
  error = "",
  onRetry,
}: {
  events: RunEvent[];
  blind: boolean;
  /** False until the first fetch worked: an empty list means nothing yet. */
  loaded?: boolean;
  /** The last failed fetch, "" when it worked. */
  error?: string;
  onRetry?: () => void;
}) {
  const { t, language } = useLocale();
  const locale = language === "zh" ? "zh-CN" : "en";
  // The newest event is the last line: follow it while the reader is at the
  // end, leave the scroll alone once they scrolled up to read older ones.
  const list = useRef<HTMLOListElement>(null);
  const atEnd = useRef(true);
  const last = events.length ? events[events.length - 1].seq : null;
  useEffect(() => {
    const node = list.current;
    if (node && atEnd.current) node.scrollTop = node.scrollHeight;
  }, [last, events.length]);
  const days = new Set(events.map((event) => new Date(event.at).toDateString()))
    .size;
  const failure = error && (
    <Note tone="warning" role="status" icon={CircleAlert}>
      {t("The events could not be read")} ({error})
      {onRetry && (
        <>
          {" "}
          <Button size="sm" variant="secondary" onClick={onRetry}>
            {t("Try again")}
          </Button>
        </>
      )}
    </Note>
  );
  if (events.length === 0)
    return (
      <>
        {failure}
        {!error && !loaded && (
          <p className="ar-muted">{t("automatic.run.loading")}</p>
        )}
        {!error && loaded && (
          <p className="ar-muted">{t("automatic.run.timeline.empty")}</p>
        )}
      </>
    );
  return (
    <>
      {failure}
      <ol
        ref={list}
        className="ar-timeline"
        // A scrollable region has to be reachable from the keyboard.
        tabIndex={0}
        aria-label={t("automatic.run.timeline.title")}
        onScroll={(event) => {
          const node = event.currentTarget;
          atEnd.current =
            node.scrollHeight - node.scrollTop - node.clientHeight < 24;
        }}
      >
        {events.map((raw) => {
          const event = maskEvent(raw, blind);
          const hidden = event.kind === "hidden";
          return (
            <li key={event.seq}>
              <span className="ar-muted">#{event.seq}</span>
              <time dateTime={new Date(event.at).toISOString()}>
                {days > 1
                  ? new Date(event.at).toLocaleString(locale)
                  : new Date(event.at).toLocaleTimeString(locale)}
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
    </>
  );
}
