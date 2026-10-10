// FigureSpec (schema levi.aeri.figure_spec.v1, levi/automatic/analysis/
// figspec.py) read into the plain models the chart component draws, and into
// the table that carries every number of the figure. No drawing code in the
// spec and none here: this file only decides which marks a kind has (bar,
// errorbar, point, step, line, refline), what the axes say and what the
// accessible text and table contain.

export const FIGURE_SCHEMA = "levi.aeri.figure_spec.v1";

export type SpecText = string | { en?: string; "zh-CN"?: string } | null;
export type SpecAxis = {
  label?: SpecText;
  kind?: "linear" | "category";
  categories?: SpecText[];
  min?: number;
  max?: number;
  fmt?: "plain" | "percent";
  unit?: string;
};
export type SpecPoint = {
  x: number;
  y: number;
  lo?: number;
  hi?: number;
  label?: string;
  value?: number;
};
export type SpecSeries = {
  name: SpecText;
  points: SpecPoint[];
  marker?: string;
  dash?: string;
  emphasis?: boolean;
  unavailable?: boolean;
};
export type SpecPanel = {
  title?: SpecText;
  x_axis: SpecAxis;
  y_axis: SpecAxis;
  series: SpecSeries[];
  reflines?: { axis: "x" | "y"; value: number; label?: SpecText }[];
};
export type FigureSpec = {
  schema: string;
  id: string;
  kind: string;
  lang?: string;
  title: SpecText;
  summary?: SpecText;
  notes?: SpecText[];
  panels: SpecPanel[];
};

export type Language = "en" | "zh";

/** A spec text in the page language: `zh-CN` for Chinese, else English, and
 * whatever exists when the wanted language is missing. */
export function specText(
  text: SpecText | undefined,
  language: Language,
): string {
  if (text == null) return "";
  if (typeof text === "string") return text;
  const order = language === "zh" ? ["zh-CN", "en"] : ["en", "zh-CN"];
  for (const key of order) {
    const value = (text as Record<string, string | undefined>)[key];
    if (value) return value;
  }
  return Object.values(text).find(Boolean) ?? "";
}

/** Whether `value` is a FigureSpec this page can draw. */
export function isFigureSpec(value: unknown): value is FigureSpec {
  if (!value || typeof value !== "object") return false;
  const v = value as Partial<FigureSpec>;
  return (
    v.schema === FIGURE_SCHEMA &&
    typeof v.id === "string" &&
    typeof v.kind === "string" &&
    Array.isArray(v.panels) &&
    v.panels.every(
      (p) =>
        p &&
        Array.isArray(p.series) &&
        p.x_axis &&
        p.y_axis &&
        p.series.every((s) => Array.isArray(s.points)),
    )
  );
}

export type DrawStyle = "bars" | "stack" | "forest" | "step" | "lines" | "heat";

/** How each kind is drawn (the same map as the writer's STYLE). */
export const KIND_STYLE: Record<string, DrawStyle> = {
  grouped_bar: "bars",
  early_stop: "bars",
  stacked_bar: "stack",
  forest: "forest",
  step_curve: "step",
  drift_lines: "lines",
  confusion_matrix: "heat",
};

export type Mark = "bar" | "errorbar" | "point" | "step" | "line" | "refline";

/** The marks a style uses (the vocabulary of the report's figures). */
export const STYLE_MARKS: Record<DrawStyle, Mark[]> = {
  bars: ["bar", "errorbar"],
  stack: ["bar"],
  forest: ["point", "errorbar", "refline"],
  step: ["step", "errorbar"],
  lines: ["line", "point", "errorbar"],
  heat: [],
};

export const MARKERS = [
  "circle",
  "square",
  "triangle",
  "diamond",
  "cross",
  "plus",
] as const;
export type MarkerShape = (typeof MARKERS)[number];
export const DASHES: Record<string, string> = {
  solid: "",
  dashed: "6 3",
  dotted: "1.5 2.5",
  dashdot: "6 2.5 1.5 2.5",
};

export type SeriesModel = {
  key: string; // data key in a row: s0, s1…
  index: number;
  name: string;
  marker: MarkerShape;
  dash: string; // stroke-dasharray, "" for solid
  emphasis: boolean;
  unavailable: boolean;
  /** Per-series points for line-like plots (x, y, err = [below, above]). */
  points: {
    x: number;
    y: number;
    err?: [number, number];
    lo?: number;
    hi?: number;
    label?: string;
  }[];
};

export type TableModel = { head: string[]; rows: (string | number)[][] };

export type Heat = {
  columns: string[];
  rows: {
    name: string;
    cells: { count: number; share: number; label: string }[];
  }[];
};

export type PlotModel = {
  style: DrawStyle;
  marks: Mark[];
  title: string;
  xLabel: string;
  yLabel: string;
  xFmt: "plain" | "percent";
  yFmt: "plain" | "percent";
  xCategories: string[] | null;
  yCategories: string[] | null;
  xDomain: [number | "auto", number | "auto"];
  yDomain: [number | "auto", number | "auto"];
  /** Rows of a bar plot: one per category, with s<i> and s<i>_err. */
  rows: Record<string, number | string | [number, number]>[];
  series: SeriesModel[];
  refs: { axis: "x" | "y"; value: number; label: string }[];
  heat: Heat | null;
  table: TableModel;
  unavailable: string[];
};

export type FigureModel = {
  id: string;
  kind: string;
  title: string;
  summary: string;
  notes: string[];
  plots: PlotModel[];
};

const num = (v: number, digits = 3) =>
  Number.isInteger(v)
    ? String(v)
    : String(Math.round(v * 10 ** digits) / 10 ** digits);

/** A number on an axis or in a table; rates in percent where the axis says so. */
export function formatValue(
  v: number,
  fmt: "plain" | "percent" | undefined,
): string {
  return fmt === "percent" ? `${num(v * 100, 1)}%` : num(v);
}

function categories(axis: SpecAxis, language: Language): string[] | null {
  return axis.kind === "category"
    ? (axis.categories ?? []).map((c) => specText(c, language))
    : null;
}

function labelOf(cats: string[] | null, value: number): string {
  return cats ? (cats[Math.round(value)] ?? String(value)) : num(value);
}

function buildSeries(panel: SpecPanel, language: Language): SeriesModel[] {
  return panel.series.map((s, index) => ({
    key: `s${index}`,
    index,
    name: specText(s.name, language),
    marker: (MARKERS as readonly string[]).includes(s.marker ?? "")
      ? (s.marker as MarkerShape)
      : MARKERS[index % MARKERS.length],
    dash: DASHES[s.dash ?? "solid"] ?? "",
    emphasis: Boolean(s.emphasis),
    unavailable: Boolean(s.unavailable),
    points: s.points.map((p) => ({
      x: p.x,
      y: p.y,
      lo: p.lo,
      hi: p.hi,
      label: p.label,
      err:
        p.lo != null && p.hi != null
          ? [
              Math.max(0, (isForest(panel) ? p.x : p.y) - p.lo),
              Math.max(0, p.hi - (isForest(panel) ? p.x : p.y)),
            ]
          : undefined,
    })),
  }));
}

function isForest(panel: SpecPanel): boolean {
  return panel.y_axis.kind === "category" && panel.x_axis.kind !== "category";
}

function domainOf(axis: SpecAxis): [number | "auto", number | "auto"] {
  return [axis.min ?? "auto", axis.max ?? "auto"];
}

function panelModel(
  kind: string,
  panel: SpecPanel,
  language: Language,
  t: (key: string) => string,
): PlotModel {
  const style = KIND_STYLE[kind] ?? "bars";
  const xCats = categories(panel.x_axis, language);
  const yCats = categories(panel.y_axis, language);
  const series = buildSeries(panel, language);
  const xFmt = panel.x_axis.fmt ?? "plain";
  const yFmt = panel.y_axis.fmt ?? "plain";

  const rows: PlotModel["rows"] = [];
  if (style === "bars" || style === "stack") {
    const names = xCats ?? [];
    const slots = Math.max(
      names.length,
      ...panel.series.flatMap((s) => s.points.map((p) => Math.round(p.x) + 1)),
      0,
    );
    for (let i = 0; i < slots; i += 1) {
      const row: PlotModel["rows"][number] = { name: names[i] ?? String(i) };
      for (const s of series) {
        const p = s.points.find((q) => Math.round(q.x) === i);
        if (p) {
          row[s.key] = p.y;
          if (p.err) row[`${s.key}_err`] = p.err;
          if (p.label) row[`${s.key}_label`] = p.label;
        }
      }
      rows.push(row);
    }
  }

  // The table: every number of the figure, so the figure never carries a
  // number its text does not.
  const xHead = specText(panel.x_axis.label, language) || "x";
  const yHead = specText(panel.y_axis.label, language) || "y";
  const table: TableModel = {
    head:
      style === "forest"
        ? [
            t("automatic.campaign.fig.series"),
            yHead,
            xHead,
            t("automatic.campaign.fig.low"),
            t("automatic.campaign.fig.high"),
            t("automatic.campaign.fig.note"),
          ]
        : style === "heat"
          ? []
          : [
              t("automatic.campaign.fig.series"),
              xHead,
              yHead,
              t("automatic.campaign.fig.low"),
              t("automatic.campaign.fig.high"),
              t("automatic.campaign.fig.note"),
            ],
    rows: [],
  };
  const unavailable: string[] = [];
  for (const s of series) {
    if (s.unavailable) {
      unavailable.push(s.name);
      table.rows.push([
        s.name,
        "—",
        "—",
        "—",
        "—",
        t("automatic.campaign.fig.unavailable"),
      ]);
      continue;
    }
    for (const p of s.points) {
      if (style === "forest")
        table.rows.push([
          s.name,
          labelOf(yCats, p.y),
          formatValue(p.x, xFmt),
          p.lo != null ? formatValue(p.lo, xFmt) : "—",
          p.hi != null ? formatValue(p.hi, xFmt) : "—",
          p.label ?? "",
        ]);
      else
        table.rows.push([
          s.name,
          xCats ? labelOf(xCats, p.x) : formatValue(p.x, xFmt),
          formatValue(p.y, yFmt),
          p.lo != null ? formatValue(p.lo, yFmt) : "—",
          p.hi != null ? formatValue(p.hi, yFmt) : "—",
          p.label ?? "",
        ]);
    }
  }

  let heat: Heat | null = null;
  if (style === "heat") {
    const columns = xCats ?? [];
    const names = yCats ?? [];
    const cells = names.map(() =>
      columns.map(() => ({ count: 0, share: 0, label: "" })),
    );
    for (const s of panel.series)
      for (const p of s.points) {
        const r = Math.round(p.y);
        const c = Math.round(p.x);
        if (cells[r]?.[c])
          cells[r][c] = {
            count: p.value ?? 0,
            share: 0,
            label: p.label ?? "",
          };
      }
    for (const row of cells) {
      const total = row.reduce((n, cell) => n + cell.count, 0);
      for (const cell of row) cell.share = total ? cell.count / total : 0;
    }
    heat = {
      columns,
      rows: names.map((name, i) => ({ name, cells: cells[i] })),
    };
    table.head = [yHead, ...columns];
    table.rows = heat.rows.map((r) => [
      r.name,
      ...r.cells.map((c) => `${c.count} (${formatValue(c.share, "percent")})`),
    ]);
  }

  return {
    style,
    marks: STYLE_MARKS[style],
    title: specText(panel.title, language),
    xLabel: xHead === "x" ? "" : xHead,
    yLabel: yHead === "y" ? "" : yHead,
    xFmt,
    yFmt,
    xCategories: xCats,
    yCategories: yCats,
    xDomain: domainOf(panel.x_axis),
    yDomain: domainOf(panel.y_axis),
    rows,
    series,
    refs: (panel.reflines ?? []).map((r) => ({
      axis: r.axis,
      value: r.value,
      label: specText(r.label, language),
    })),
    heat,
    table,
    unavailable,
  };
}

/** The model of a whole figure. `t` translates the few table headings. */
export function figureModel(
  spec: FigureSpec,
  language: Language,
  t: (key: string) => string = (key) => key,
): FigureModel {
  return {
    id: spec.id,
    kind: spec.kind,
    title: specText(spec.title, language),
    summary: specText(spec.summary, language),
    notes: (spec.notes ?? []).map((n) => specText(n, language)).filter(Boolean),
    plots: spec.panels.map((panel) =>
      panelModel(spec.kind, panel, language, t),
    ),
  };
}
