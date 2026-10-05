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

/** A reading in words: a mark such as ✓ or ✗ would be a glyph, not a word. */
const READING: Record<AnchoredVerdict, string> = {
  supported: "supported",
  contradicted: "contradicted",
  unknown: "unknown",
};

/**
 * The tooltip body of an event: the answers, then each condition with its
 * reading in words. `translate` renders the reading in the interface language.
 */
export function eventText(
  event: AnchoredEvent,
  translate: (text: string) => string = (text) => text,
): string {
  const answers = Object.entries(event.answer)
    .map(([field, value]) => `${field}: ${value}`)
    .join(" · ");
  const checks = event.checks
    .map((c) => `${c.field} — ${translate(READING[c.result] ?? "unknown")}`)
    .join(" · ");
  return checks ? `${answers}\n${checks}` : answers;
}

export function anchoredMarkers(
  record: AnchoredEpisode | null | undefined,
  duration: number,
  translate?: (text: string) => string,
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
    text: eventText(event, translate),
  }));
}

/** The spec's display name in `language` (else English, else any), falling
 * back to its id when the spec has no title. */
export function anchoredSpecTitle(
  spec: AnchoredEpisode["spec"],
  language: string,
): string {
  const title = spec.title ?? {};
  return (
    title[language] || title.en || Object.values(title).find(Boolean) || spec.id
  );
}

/** "2/3 valid · pink, white" — the section header's summary. */
export function anchoredSummary(record: AnchoredEpisode): string {
  const valid = record.events.filter((e) => e.valid).length;
  const labels = record.basis.valid_labels;
  return `${valid}/${record.events.length} valid${labels && labels.length ? ` · ${labels.join(", ")}` : ""}`;
}
