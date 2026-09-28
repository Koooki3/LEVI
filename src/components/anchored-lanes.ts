/**
 * Pure geometry and wording for the ANCHORED REVIEW row of the annotations
 * timeline: one marker per recorded robot event, placed at its time and
 * coloured by the event's verdict, with the model's answers as the tooltip.
 */

import type {
  AnchoredEpisode,
  AnchoredEvent,
  AnchoredVerdict,
} from "@/types/anchored.types";

export interface AnchoredMarker {
  /** Seconds into the episode (the anchor frame's timestamp). */
  t: number;
  /** Left edge as a percentage of the track, or null past the video's end. */
  left: number | null;
  verdict: AnchoredVerdict;
  /** Tooltip header: event, time and frame. */
  meta: string;
  /** Tooltip body: the answers, then each condition's reading. */
  text: string;
}

const MARK: Record<AnchoredVerdict, string> = {
  supported: "✓",
  contradicted: "✗",
  unknown: "?",
};

export function eventText(event: AnchoredEvent): string {
  const answers = Object.entries(event.answer)
    .map(([field, value]) => `${field}: ${value}`)
    .join(" · ");
  const checks = event.checks
    .map((c) => `${MARK[c.result] ?? "?"} ${c.field}`)
    .join("  ");
  return checks ? `${answers}\n${checks}` : answers;
}

export function anchoredMarkers(
  record: AnchoredEpisode | null | undefined,
  duration: number,
): AnchoredMarker[] {
  if (!record) return [];
  return record.events.map((event) => ({
    t: event.timestamp,
    left:
      duration > 0 && event.timestamp <= duration
        ? (event.timestamp / duration) * 100
        : null,
    verdict: event.verdict,
    meta: `${record.event} · ${event.timestamp.toFixed(2)}s · f${event.frame_index} · ${event.valid ? "valid" : event.verdict}`,
    text: eventText(event),
  }));
}

/** "2/3 valid · pink, white" — the section header's summary. */
export function anchoredSummary(record: AnchoredEpisode): string {
  const valid = record.events.filter((e) => e.valid).length;
  const labels = record.basis.valid_labels;
  return `${valid}/${record.events.length} valid${labels && labels.length ? ` · ${labels.join(", ")}` : ""}`;
}
