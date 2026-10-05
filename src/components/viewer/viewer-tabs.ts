/**
 * The episode viewer's tabs (design stage 3). "Action insights", "Filtering"
 * and "Doctor" used to be three top-level tabs; they are now the three views
 * of one "Analysis" tab. Older sessions stored the old tab ids, so a stored
 * "insights" / "filtering" / "doctor" still opens that view.
 */

export type ViewerTab =
  | "episodes"
  | "annotations"
  | "urdf"
  | "statistics"
  | "frames"
  | "analysis";

export type AnalysisView = "insights" | "filtering" | "doctor";

export const VIEWER_TABS: readonly ViewerTab[] = [
  "episodes",
  "annotations",
  "urdf",
  "statistics",
  "frames",
  "analysis",
];

export const ANALYSIS_VIEWS: readonly AnalysisView[] = [
  "insights",
  "filtering",
  "doctor",
];

function isAnalysisView(value: string | null): value is AnalysisView {
  return (
    value !== null && (ANALYSIS_VIEWS as readonly string[]).includes(value)
  );
}

/**
 * The tab and analysis view to open from what the session stored. An old
 * tab id that is now an analysis view opens "analysis" on that view; an
 * unknown value opens the episodes tab.
 */
export function restoreViewerTab(
  storedTab: string | null,
  storedView: string | null,
): { tab: ViewerTab; view: AnalysisView } {
  const view: AnalysisView = isAnalysisView(storedView)
    ? storedView
    : "insights";
  if (isAnalysisView(storedTab)) return { tab: "analysis", view: storedTab };
  if (storedTab && (VIEWER_TABS as readonly string[]).includes(storedTab)) {
    return { tab: storedTab as ViewerTab, view };
  }
  return { tab: "episodes", view };
}

/**
 * What a tab needs loaded when it opens, exactly as the separate tabs did:
 * statistics need the episode lengths, the frame gallery the frame index,
 * action insights the cross-episode variance, filtering both lengths and
 * variance; the doctor loads its own report.
 */
export function loadsFor(
  tab: ViewerTab,
  view: AnalysisView,
): { stats: boolean; frames: boolean; insights: boolean } {
  const analysis = tab === "analysis";
  return {
    stats: tab === "statistics" || (analysis && view === "filtering"),
    frames: tab === "frames",
    insights: analysis && (view === "insights" || view === "filtering"),
  };
}

/** Whether the episode list is shown beside this tab. */
export function showsEpisodeList(tab: ViewerTab): boolean {
  return tab === "episodes" || tab === "annotations" || tab === "urdf";
}

/**
 * The episode before (`delta` −1) or after (+1) `current` in `episodes`;
 * from an episode outside the list, the first (forward) or last (back).
 * `undefined` at either end.
 */
export function adjacentEpisode(
  episodes: readonly number[],
  current: number,
  delta: 1 | -1,
): number | undefined {
  const index = episodes.indexOf(current);
  const next =
    index === -1 ? (delta > 0 ? 0 : episodes.length - 1) : index + delta;
  return episodes[next];
}
