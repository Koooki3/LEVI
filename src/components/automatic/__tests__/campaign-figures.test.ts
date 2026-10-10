import { describe, expect, test } from "bun:test";
import {
  FIGURE_SCHEMA,
  figureModel,
  formatValue,
  isFigureSpec,
  specText,
  type FigureSpec,
} from "../campaign-figures";
import {
  basisFromQuery,
  downloadGroups,
  figureFiles,
  summaryFile,
} from "../campaign-report-logic";

const base = (over: Partial<FigureSpec>): FigureSpec => ({
  schema: FIGURE_SCHEMA,
  id: "f",
  kind: "grouped_bar",
  title: { en: "Success rate", "zh-CN": "成功率" },
  summary: { en: "B is higher", "zh-CN": "B 更高" },
  panels: [],
  ...over,
});

const cats = ["Arm A", "Arm B"];

export const grouped = base({
  id: "f1-success",
  kind: "grouped_bar",
  panels: [
    {
      x_axis: { kind: "category", categories: cats, label: "Arm" },
      y_axis: {
        kind: "linear",
        fmt: "percent",
        min: 0,
        max: 1,
        label: "Success rate",
      },
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
});

const t = (k: string) => k;

describe("which marks a figure kind draws", () => {
  test("grouped bars: bars with error bars and k/n labels", () => {
    const m = figureModel(grouped, "en", t);
    const plot = m.plots[0];
    expect(plot.style).toBe("bars");
    expect(plot.marks).toEqual(["bar", "errorbar"]);
    expect(plot.rows).toEqual([
      {
        name: "Arm A",
        s0: 0.4,
        s0_err: [0.4 - 0.25, 0.57 - 0.4],
        s0_label: "8/20",
      },
      {
        name: "Arm B",
        s0: 0.7,
        s0_err: [0.7 - 0.5, 0.84 - 0.7],
        s0_label: "14/20",
      },
    ]);
    expect(plot.yDomain).toEqual([0, 1]);
    expect(formatValue(0.4, plot.yFmt)).toBe("40%");
  });

  test("stacked bars: bars only, no interval", () => {
    const spec = base({
      kind: "stacked_bar",
      panels: [
        {
          x_axis: { kind: "category", categories: cats },
          y_axis: { kind: "linear" },
          series: [
            {
              name: "Dropped",
              points: [
                { x: 0, y: 2 },
                { x: 1, y: 1 },
              ],
            },
            { name: "Missed", points: [{ x: 0, y: 3 }] },
          ],
        },
      ],
    });
    const plot = figureModel(spec, "en", t).plots[0];
    expect(plot.style).toBe("stack");
    expect(plot.marks).toEqual(["bar"]);
    expect(plot.rows[1]).toEqual({ name: "Arm B", s0: 1 });
  });

  test("a forest plot: points, horizontal intervals around x, a reference line", () => {
    const spec = base({
      kind: "forest",
      panels: [
        {
          x_axis: { kind: "linear", fmt: "percent", label: "Difference" },
          y_axis: { kind: "category", categories: ["B - A", "C - A"] },
          series: [
            {
              name: "Newcombe",
              points: [
                { x: 0.3, y: 0, lo: 0.05, hi: 0.5 },
                { x: -0.1, y: 1, lo: -0.3, hi: 0.1 },
              ],
            },
          ],
          reflines: [{ axis: "x", value: 0, label: "no difference" }],
        },
      ],
    });
    const plot = figureModel(spec, "en", t).plots[0];
    expect(plot.style).toBe("forest");
    expect(plot.marks).toEqual(["point", "errorbar", "refline"]);
    expect(plot.series[0].points[0].err).toEqual([0.3 - 0.05, 0.5 - 0.3]);
    expect(plot.refs).toEqual([
      { axis: "x", value: 0, label: "no difference" },
    ]);
    expect(plot.yCategories).toEqual(["B - A", "C - A"]);
    // The table names the comparison, not an index.
    expect(plot.table.rows[0].slice(0, 3)).toEqual([
      "Newcombe",
      "B - A",
      "30%",
    ]);
  });

  test("a forest plot's x axis follows the data, the interval ends and the zero line", () => {
    const spec = base({
      kind: "forest",
      panels: [
        {
          x_axis: { kind: "linear", fmt: "plain" },
          y_axis: { kind: "category", categories: ["B - A"] },
          series: [
            { name: "Newcombe", points: [{ x: 0, y: 0, lo: -0.49, hi: 0.49 }] },
            { name: "Bootstrap", points: [{ x: 0, y: 0, lo: -0.4, hi: 0.45 }] },
          ],
          reflines: [{ axis: "x", value: 0 }],
        },
      ],
    });
    const [lo, hi] = figureModel(spec, "en", t).plots[0].xDomain as [
      number,
      number,
    ];
    // Covers -0.49..0.49 with a margin, and nothing like 0..4.
    expect(lo).toBeLessThan(-0.49);
    expect(lo).toBeGreaterThan(-0.7);
    expect(hi).toBeGreaterThan(0.49);
    expect(hi).toBeLessThan(0.7);
    // A bound the spec names is kept.
    spec.panels[0].x_axis.max = 1;
    expect(figureModel(spec, "en", t).plots[0].xDomain[1]).toBe(1);
  });

  test("a step curve and a drift plot", () => {
    const step = figureModel(
      base({
        kind: "step_curve",
        panels: [
          {
            x_axis: { kind: "linear", label: "Steps" },
            y_axis: { kind: "linear", fmt: "percent", min: 0, max: 1 },
            series: [
              {
                name: "A",
                emphasis: true,
                points: [
                  { x: 0, y: 0 },
                  { x: 100, y: 0.5, lo: 0.3, hi: 0.7 },
                ],
              },
            ],
          },
        ],
      }),
      "en",
      t,
    ).plots[0];
    expect(step.style).toBe("step");
    expect(step.marks).toEqual(["step", "errorbar"]);
    expect(step.series[0].emphasis).toBe(true);
    expect(step.series[0].points[0].err).toBeUndefined();

    const drift = figureModel(
      base({
        kind: "drift_lines",
        panels: [
          {
            x_axis: { kind: "linear" },
            y_axis: { kind: "linear", fmt: "percent" },
            series: [
              { name: "A", dash: "dashed", points: [{ x: 1, y: 0.5 }] },
              { name: "B", points: [{ x: 1, y: 0.6 }] },
            ],
          },
        ],
      }),
      "en",
      t,
    ).plots[0];
    expect(drift.marks).toContain("line");
    // Series differ by marker and dash, not only by colour.
    expect(drift.series[0].dash).toBe("6 3");
    expect(drift.series[0].marker).not.toBe(drift.series[1].marker);
  });

  test("a confusion matrix is a table of counts with row shares", () => {
    const spec = base({
      kind: "confusion_matrix",
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
                { x: 0, y: 0, value: 6 },
                { x: 1, y: 0, value: 2 },
                { x: 0, y: 1, value: 0 },
                { x: 1, y: 1, value: 4 },
              ],
            },
          ],
        },
      ],
    });
    const plot = figureModel(spec, "en", t).plots[0];
    expect(plot.style).toBe("heat");
    expect(plot.heat!.rows[0].cells.map((c) => c.count)).toEqual([6, 2]);
    expect(plot.heat!.rows[0].cells[0].share).toBeCloseTo(0.75);
    expect(plot.heat!.rows[1].cells[1].share).toBe(1);
  });

  test("a series with no data keeps its name and a row in the table", () => {
    const spec = base({
      panels: [
        {
          x_axis: { kind: "category", categories: cats },
          y_axis: { kind: "linear" },
          series: [
            { name: "A", points: [{ x: 0, y: 1 }] },
            { name: "D", unavailable: true, points: [] },
          ],
        },
      ],
    });
    const plot = figureModel(spec, "en", t).plots[0];
    expect(plot.unavailable).toEqual(["D"]);
    expect(plot.table.rows.some((r) => r[0] === "D")).toBe(true);
  });
});

describe("text and validation", () => {
  test("Chinese text is used in Chinese and English otherwise", () => {
    expect(specText({ en: "a", "zh-CN": "甲" }, "zh")).toBe("甲");
    expect(specText({ en: "a", "zh-CN": "甲" }, "en")).toBe("a");
    expect(specText({ en: "a" }, "zh")).toBe("a");
    expect(figureModel(grouped, "zh", t).title).toBe("成功率");
  });

  test("only a levi.aeri.figure_spec.v1 with panels is drawn", () => {
    expect(isFigureSpec(grouped)).toBe(true);
    expect(isFigureSpec({ ...grouped, schema: "other" })).toBe(false);
    expect(isFigureSpec({ ...grouped, panels: [{}] })).toBe(false);
    expect(isFigureSpec(null)).toBe(false);
  });
});

describe("the report's files", () => {
  const files = [
    { name: "figures/f2-differences.json", kind: "figure" },
    { name: "figures/f1-success.json", kind: "figure" },
    { name: "figures/f1-success.svg", kind: "figure" },
    { name: "tables/success.csv", kind: "table" },
    { name: "data/analysis.json", kind: "data" },
    { name: "summary.en.md", kind: "summary" },
    { name: "summary.zh-CN.md", kind: "summary" },
    { name: "manifest.json", kind: "manifest" },
  ];
  test("figure specs are the figures/*.json files, in name order", () => {
    expect(figureFiles(files).map((f) => f.name)).toEqual([
      "figures/f1-success.json",
      "figures/f2-differences.json",
    ]);
  });
  test("the summary follows the page language", () => {
    expect(summaryFile(files, "zh")!.name).toBe("summary.zh-CN.md");
    expect(summaryFile(files, "en")!.name).toBe("summary.en.md");
    expect(summaryFile([], "en")).toBeNull();
  });
  test("downloads are grouped and leave the summaries to the page", () => {
    const groups = downloadGroups(files);
    expect(groups.map((g) => g.key)).toEqual([
      "tables",
      "figures",
      "data",
      "other",
    ]);
    expect(
      groups.flatMap((g) => g.files).some((f) => f.name.startsWith("summary")),
    ).toBe(false);
  });
  test("an unknown basis in the address falls back", () => {
    expect(
      basisFromQuery(
        "adjudicated_ground_truth",
        ["operator_label", "adjudicated_ground_truth"],
        "operator_label",
      ),
    ).toBe("adjudicated_ground_truth");
    expect(basisFromQuery("nope", ["operator_label"], "operator_label")).toBe(
      "operator_label",
    );
    expect(basisFromQuery(null, ["operator_label"], "operator_label")).toBe(
      "operator_label",
    );
  });
});
