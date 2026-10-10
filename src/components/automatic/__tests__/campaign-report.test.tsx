import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { CampaignReportView } = await import("../campaign-report");
const { FigureCard } = await import("../campaign-charts");
import { ApiError, type WizardApi } from "../wizard-api";
import { FIGURE_SCHEMA, type FigureSpec } from "../campaign-figures";

setupDom();

const cats = ["Arm A", "Arm B"];
const bar: FigureSpec = {
  schema: FIGURE_SCHEMA,
  id: "f1-success",
  kind: "grouped_bar",
  title: { en: "Success rate by arm" },
  summary: { en: "Arm B succeeded more often than Arm A." },
  notes: [{ en: "Intervals are Wilson intervals." }],
  panels: [
    {
      x_axis: { kind: "category", categories: cats },
      y_axis: { kind: "linear", fmt: "percent", min: 0, max: 1, label: "Rate" },
      series: [
        {
          name: "Success",
          points: [
            { x: 0, y: 0.4, lo: 0.25, hi: 0.57, label: "8/20" },
            { x: 1, y: 0.7, lo: 0.5, hi: 0.84, label: "14/20" },
          ],
        },
      ],
    },
  ],
};
const forest: FigureSpec = {
  ...bar,
  id: "f2-differences",
  kind: "forest",
  title: { en: "Differences" },
  summary: { en: "B minus A." },
  panels: [
    {
      x_axis: { kind: "linear", fmt: "percent", label: "Difference" },
      y_axis: { kind: "category", categories: ["B - A"] },
      series: [
        { name: "Newcombe", points: [{ x: 0.3, y: 0, lo: 0.05, hi: 0.5 }] },
      ],
      reflines: [{ axis: "x", value: 0 }],
    },
  ],
};
const step: FigureSpec = {
  ...bar,
  id: "f3",
  kind: "step_curve",
  title: { en: "Time to success" },
  panels: [
    {
      x_axis: { kind: "linear", label: "Steps" },
      y_axis: { kind: "linear", fmt: "percent", min: 0, max: 1 },
      series: [
        {
          name: "A",
          points: [
            { x: 0, y: 0 },
            { x: 50, y: 0.5, lo: 0.3, hi: 0.7 },
          ],
        },
        {
          name: "B",
          emphasis: true,
          points: [
            { x: 0, y: 0 },
            { x: 40, y: 0.6 },
          ],
        },
      ],
    },
  ],
};
const heat: FigureSpec = {
  ...bar,
  id: "f7",
  kind: "confusion_matrix",
  title: { en: "Agreement" },
  panels: [
    {
      title: "Arm A",
      x_axis: {
        kind: "category",
        categories: ["success", "failure"],
        label: "Automatic",
      },
      y_axis: {
        kind: "category",
        categories: ["success", "failure"],
        label: "Operator",
      },
      series: [
        {
          name: "cells",
          points: [
            { x: 0, y: 0, value: 3 },
            { x: 1, y: 1, value: 1 },
          ],
        },
      ],
    },
  ],
};

describe("a figure", () => {
  test("hides the plot from assistive technology and says the same in words and a table", async () => {
    const { host } = await render(<FigureCard spec={bar} />);
    const plot = host.querySelector(".ac-plot")!;
    expect(plot.getAttribute("aria-hidden")).toBe("true");
    expect(plot.querySelector("svg")).not.toBeNull();
    expect(plot.querySelectorAll(".recharts-bar-rectangle").length).toBe(2);
    expect(host.textContent).toContain("Success rate by arm");
    expect(host.textContent).toContain(
      "Arm B succeeded more often than Arm A.",
    );
    // The legend names the series in text.
    expect(host.querySelector(".ac-legend")!.textContent).toContain("Success");
    // The table carries every number: values, interval ends, k/n.
    const table = host.querySelector("details.ac-fig-table table")!;
    expect(table.textContent).toContain("40%");
    expect(table.textContent).toContain("25%");
    expect(table.textContent).toContain("57%");
    expect(table.textContent).toContain("8/20");
    expect(host.textContent).toContain("Intervals are Wilson intervals.");
  });

  test("forest, step and heat figures draw", async () => {
    const f = await render(<FigureCard spec={forest} />);
    expect(f.host.querySelector(".ac-plot svg")).not.toBeNull();
    expect(f.host.querySelector("details table")!.textContent).toContain(
      "B - A",
    );
    const s = await render(<FigureCard spec={step} />);
    expect(s.host.querySelectorAll(".recharts-line").length).toBe(2);
    expect(s.host.querySelector(".ac-legend")!.textContent).toContain("B");
    const h = await render(<FigureCard spec={heat} />);
    expect(h.host.querySelector("table.ac-heat")).not.toBeNull();
    expect(h.host.querySelector(".ac-plot")).toBeNull();
    expect(h.host.textContent).toContain("Automatic: success");
  });
});

function reportApi(over: Partial<WizardApi> = {}) {
  const files = [
    { name: "figures/f1-success.json", kind: "figure" },
    { name: "figures/f2-differences.json", kind: "figure" },
    { name: "tables/success.csv", kind: "table" },
    { name: "summary.en.md", kind: "summary" },
  ];
  return {
    getCampaignReport: mock(async (_id: string, basis?: string) => ({
      basis: basis ?? "operator_label",
      files,
      analysis: {
        automatic: basis === "autonomous_verdict",
        basis_name: { en: "success rate (operator label)", "zh-CN": "成功率" },
      },
      manifest: { campaign_sha256: "abc" },
    })),
    getReportFileJson: mock(async (_id: string, name: string) =>
      name.includes("f1") ? bar : forest,
    ),
    getReportFileText: mock(async () => "## Result\n\nB is higher than A."),
    ...over,
  } as unknown as Pick<
    WizardApi,
    "getCampaignReport" | "getReportFileJson" | "getReportFileText"
  >;
}

describe("the report page", () => {
  test("shows figures, the summary, downloads and the manifest", async () => {
    const api = reportApi();
    const { host } = await render(
      <CampaignReportView
        campaignId="camp-1"
        basis="operator_label"
        onBasisChange={() => {}}
        api={api}
      />,
    );
    await waitFor(
      () => host.querySelectorAll("figure.ac-figure").length === 2,
      {
        label: "both figures",
      },
    );
    expect(host.textContent).toContain("B is higher than A.");
    expect(
      host.querySelector('a[href*="report/files/tables/success.csv"]'),
    ).not.toBeNull();
    expect(host.querySelector(".ac-manifest")!.textContent).toContain(
      "campaign_sha256",
    );
    expect(host.textContent).toContain("success rate (operator label)");
    expect(host.textContent).not.toContain("nobody has reviewed");
  });

  test("a blinded campaign shows no results and fetches no figure", async () => {
    const api = reportApi({
      getCampaignReport: mock(async () => {
        throw new ApiError(409, "blinded", "blinded");
      }) as unknown as WizardApi["getCampaignReport"],
    });
    const { host } = await render(
      <CampaignReportView
        campaignId="camp-1"
        basis="operator_label"
        onBasisChange={() => {}}
        api={api}
      />,
    );
    await waitFor(() =>
      host.textContent?.includes("not shown before the evaluation is finished"),
    );
    expect(host.querySelector("figure")).toBeNull();
    expect(
      (api.getReportFileJson as ReturnType<typeof mock>).mock.calls.length,
    ).toBe(0);
  });

  test("an automatic basis is named unreviewed and warned about", async () => {
    const { host } = await render(
      <CampaignReportView
        campaignId="camp-1"
        basis="autonomous_verdict"
        onBasisChange={() => {}}
        api={reportApi()}
      />,
    );
    await waitFor(() => host.textContent?.includes("nobody has reviewed"));
  });

  test("changing the basis tells the page", async () => {
    const seen: string[] = [];
    const { host } = await render(
      <CampaignReportView
        campaignId="camp-1"
        basis="operator_label"
        onBasisChange={(b) => seen.push(b)}
        api={reportApi()}
      />,
    );
    await waitFor(() => host.querySelector("figure"));
    const select = host.querySelector("select")!;
    await act(async () => {
      select.value = "adjudicated_ground_truth";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(seen).toEqual(["adjudicated_ground_truth"]);
  });

  test("a figure that cannot be read is reported, the others still show", async () => {
    const api = reportApi({
      getReportFileJson: mock(async (_id: string, name: string) => {
        if (name.includes("f2")) throw new Error("HTTP 404");
        return bar;
      }) as unknown as WizardApi["getReportFileJson"],
    });
    const { host } = await render(
      <CampaignReportView
        campaignId="camp-1"
        basis="operator_label"
        onBasisChange={() => {}}
        api={api}
      />,
    );
    await waitFor(() =>
      host.textContent?.includes("Some figures could not be read"),
    );
    expect(host.querySelectorAll("figure.ac-figure").length).toBe(1);
  });
});
