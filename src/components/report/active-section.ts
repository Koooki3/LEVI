/**
 * Which section the contents column marks. A section is "current" once its
 * heading has climbed above the reading line, about 40 % down the window:
 * what fills the screen is the section whose heading just went by, not the
 * one before it (a line at the top bar's edge marks a section only after
 * most of the next one is already on screen).
 */
export const READING_LINE_FRACTION = 0.4;

/** The line, in px from the top of the window. */
export function readingLine(viewportHeight: number, stickyTop = 0): number {
  return Math.max(viewportHeight * READING_LINE_FRACTION, stickyTop + 120);
}

/**
 * The index of the last heading whose top is above the line, or -1 when
 * none is (the reader is above the first heading). `tops` are the headings'
 * `getBoundingClientRect().top`, null for one that is not in the page.
 */
export function currentHeadingIndex(
  tops: Array<number | null>,
  line: number,
): number {
  let found = -1;
  tops.forEach((top, index) => {
    if (top !== null && top < line) found = index;
  });
  return found;
}

/** The top bar's height while it stays in view (shell.css). */
export function stickyTop(): number {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue("--levi-sticky-top")
    .trim();
  const px = Number.parseFloat(value);
  return Number.isFinite(px) ? px : 0;
}
