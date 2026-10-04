import { seriesColor, seriesDash } from "./data-palette";

/** A legend mark for series `index`: its colour and its line pattern. */
export function SeriesSwatch({ index }: { index: number }) {
  return (
    <svg width="16" height="4" aria-hidden="true" className="shrink-0">
      <line
        x1="0"
        y1="2"
        x2="16"
        y2="2"
        stroke={seriesColor(index)}
        strokeWidth="2.5"
        strokeDasharray={seriesDash(index)}
      />
    </svg>
  );
}
