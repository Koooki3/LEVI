"use client";
import { useEffect, useState, type MutableRefObject } from "react";
import { loadsFor, type AnalysisView, type ViewerTab } from "./viewer-tabs";

/** What the viewer can load lazily for a tab. */
export type TabLoaders = {
  stats: () => void;
  frames: () => void;
  insights: () => void;
};

/**
 * The viewer's tab and analysis-view state. Opening a tab or an analysis
 * view (and the restored tab on mount) runs the loaders it needs
 * (`loadsFor`), as the separate tabs did. `loaders` is a ref so the viewer
 * can hand over loaders defined later in its render.
 */
export function useViewerTabs(
  initial: { tab: ViewerTab; view: AnalysisView },
  loaders: MutableRefObject<TabLoaders>,
) {
  const [activeTab, setActiveTab] = useState<ViewerTab>(initial.tab);
  const [analysisView, setAnalysisView] = useState<AnalysisView>(initial.view);
  const run = (tab: ViewerTab, view: AnalysisView) => {
    const needs = loadsFor(tab, view);
    if (needs.stats) loaders.current.stats();
    if (needs.frames) loaders.current.frames();
    if (needs.insights) loaders.current.insights();
  };
  // Load what the restored tab needs, once.
  useEffect(() => {
    run(initial.tab, initial.view);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return {
    activeTab,
    analysisView,
    changeTab: (tab: ViewerTab) => {
      setActiveTab(tab);
      run(tab, analysisView);
    },
    changeView: (view: AnalysisView) => {
      setAnalysisView(view);
      run("analysis", view);
    },
  };
}
