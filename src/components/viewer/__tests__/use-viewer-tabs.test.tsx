import { click, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { useRef } from "react";
import { useViewerTabs, type TabLoaders } from "../use-viewer-tabs";
import type { AnalysisView, ViewerTab } from "../viewer-tabs";

setupDom();

function Harness({
  tab,
  view,
  loaders,
}: {
  tab: ViewerTab;
  view: AnalysisView;
  loaders: TabLoaders;
}) {
  const ref = useRef(loaders);
  const t = useViewerTabs({ tab, view }, ref);
  return (
    <div>
      <p data-testid="state">
        {t.activeTab}/{t.analysisView}
      </p>
      <button data-go="stats" onClick={() => t.changeTab("statistics")} />
      <button data-go="frames" onClick={() => t.changeTab("frames")} />
      <button data-go="analysis" onClick={() => t.changeTab("analysis")} />
      <button data-go="filtering" onClick={() => t.changeView("filtering")} />
      <button data-go="doctor" onClick={() => t.changeView("doctor")} />
      <button data-go="insights" onClick={() => t.changeView("insights")} />
    </div>
  );
}

function loaders() {
  return {
    stats: mock(() => undefined),
    frames: mock(() => undefined),
    insights: mock(() => undefined),
  };
}

const go = (host: HTMLElement, name: string) =>
  click(host.querySelector(`[data-go="${name}"]`));

describe("viewer tabs load their data", () => {
  test("the restored tab loads on mount", async () => {
    const l = loaders();
    await render(<Harness tab="analysis" view="filtering" loaders={l} />);
    expect(l.stats).toHaveBeenCalledTimes(1);
    expect(l.insights).toHaveBeenCalledTimes(1);
    expect(l.frames).not.toHaveBeenCalled();
  });

  test("switching the analysis view loads what that view needs", async () => {
    const l = loaders();
    const { host } = await render(
      <Harness tab="analysis" view="doctor" loaders={l} />,
    );
    expect(l.insights).not.toHaveBeenCalled();
    await go(host, "insights");
    expect(l.insights).toHaveBeenCalledTimes(1);
    expect(l.stats).not.toHaveBeenCalled();
    await go(host, "filtering");
    expect(l.stats).toHaveBeenCalledTimes(1);
    expect(l.insights).toHaveBeenCalledTimes(2);
    await go(host, "doctor");
    expect(l.insights).toHaveBeenCalledTimes(2);
    expect(host.textContent).toContain("analysis/doctor");
  });

  test("opening tabs loads statistics, frames, and the current analysis view", async () => {
    const l = loaders();
    const { host } = await render(
      <Harness tab="episodes" view="filtering" loaders={l} />,
    );
    expect(l.stats).not.toHaveBeenCalled();
    await go(host, "stats");
    expect(l.stats).toHaveBeenCalledTimes(1);
    await go(host, "frames");
    expect(l.frames).toHaveBeenCalledTimes(1);
    await go(host, "analysis");
    expect(l.stats).toHaveBeenCalledTimes(2);
    expect(l.insights).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("analysis/filtering");
  });
});
