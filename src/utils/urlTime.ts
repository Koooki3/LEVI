/**
 * The episode page mirrors the playhead into `?t=<whole seconds>` (so a
 * link reopens near the same moment) and seeks when `?t=` changes (a shared
 * link, a Workbench jump). Next.js feeds `history.replaceState` back into
 * `useSearchParams`, so without this guard every paused seek — a click on a
 * timeline segment at 26.9 s — was written as `t=26` and then re-sought to
 * 26 s: up to a second (9 frames at 10 fps) off.
 *
 * `urlTimeWriter` remembers the value the page itself last wrote;
 * `urlSeekTarget` ignores exactly that value and parses anything else.
 */
export const urlTimeWriter: { last: string | null } = { last: null };

export function urlSeekTarget(
  timeParam: string | null,
  selfWritten: string | null = urlTimeWriter.last,
): number | null {
  if (!timeParam || timeParam === selfWritten) return null;
  const value = parseFloat(timeParam);
  return Number.isFinite(value) ? value : null;
}
