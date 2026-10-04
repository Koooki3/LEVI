import { click, press, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { useState } from "react";
import { AnalysisTab } from "../analysis-tab";
import type { AnalysisView } from "../viewer-tabs";

setupDom();

function Harness({
  initial = "insights",
  onChange,
}: {
  initial?: AnalysisView;
  onChange?: (view: AnalysisView) => void;
}) {
  const [view, setView] = useState<AnalysisView>(initial);
  return (
    <AnalysisTab
      view={view}
      onViewChange={(next) => {
        setView(next);
        onChange?.(next);
      }}
    >
      {(current) => <p data-testid="view">{current}</p>}
    </AnalysisTab>
  );
}

describe("Analysis tab", () => {
  test("offers the three former tabs as one radio group", async () => {
    const { host } = await render(<Harness />);
    const group = host.querySelector('[role="radiogroup"]')!;
    expect(group.getAttribute("aria-label")).toBe("Analysis view");
    const options = [...group.querySelectorAll('[role="radio"]')];
    expect(options.map((o) => o.textContent)).toEqual([
      "Action Insights",
      "Filtering",
      "Doctor",
    ]);
    expect(options[0].getAttribute("aria-checked")).toBe("true");
    expect(host.querySelector('[data-testid="view"]')!.textContent).toBe(
      "insights",
    );
  });

  test("choosing a view renders it and reports the choice", async () => {
    const onChange = mock((view: AnalysisView) => void view);
    const { host } = await render(<Harness onChange={onChange} />);
    const options = host.querySelectorAll('[role="radio"]');
    await click(options[2]);
    expect(onChange).toHaveBeenCalledWith("doctor");
    expect(host.querySelector('[data-testid="view"]')!.textContent).toBe(
      "doctor",
    );
    expect(
      host
        .querySelector("[data-analysis-view]")!
        .getAttribute("data-analysis-view"),
    ).toBe("doctor");
  });

  test("arrow keys move between the views", async () => {
    const onChange = mock((view: AnalysisView) => void view);
    const { host } = await render(
      <Harness initial="filtering" onChange={onChange} />,
    );
    const checked = host.querySelector('[aria-checked="true"]')!;
    expect(checked.textContent).toBe("Filtering");
    await press(checked, "ArrowRight");
    expect(onChange).toHaveBeenLastCalledWith("doctor");
    await press(host.querySelector('[aria-checked="true"]'), "ArrowLeft");
    expect(onChange).toHaveBeenLastCalledWith("filtering");
  });

  test("describes the chosen view in words", async () => {
    const { host } = await render(<Harness initial="doctor" />);
    expect(host.textContent).toContain("lerobot-doctor");
  });
});

describe("Analysis tab loading", () => {
  test("the view sits in its own positioned box, below the switch", async () => {
    const { host } = await render(<Harness />);
    const body = host.querySelector("[data-analysis-view]")!;
    expect(body.className).toContain("vw-analysis-body");
    expect(body.contains(host.querySelector('[role="radiogroup"]'))).toBe(
      false,
    );
  });
});
