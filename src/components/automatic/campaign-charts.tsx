"use client";
// FigureSpec figures drawn with Recharts, the way the RECAP comparison charts
// are (recap-compare-charts.tsx): the plot is hidden from assistive
// technology, every figure has its summary in words and a table with each
// number, colours come from design tokens through CSS classes, and a series
// is always named in text and has its own marker shape and line style, so it
// never rests on colour alone.
import { useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  ErrorBar,
  LabelList,
  Line,
  ReferenceLine,
  Scatter,
  ScatterChart,
  XAxis,
  YAxis,
} from "recharts";
import { useLocale } from "@/components/levi-locale";
import {
  figureModel,
  formatValue,
  type FigureSpec,
  type MarkerShape,
  type PlotModel,
  type SeriesModel,
  type TableModel,
} from "./campaign-figures";

const CHART_H = 230;
const FALLBACK_W = 420;

function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(FALLBACK_W);
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? 0);
      if (next > 0) setWidth(next);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

/** A marker of one of the six shapes, centred on (cx, cy). */
export function markerPath(
  shape: MarkerShape,
  cx: number,
  cy: number,
  r = 4.5,
) {
  switch (shape) {
    case "square":
      return `M${cx - r},${cy - r}h${2 * r}v${2 * r}h${-2 * r}z`;
    case "triangle":
      return `M${cx},${cy - r}L${cx + r},${cy + r}L${cx - r},${cy + r}z`;
    case "diamond":
      return `M${cx},${cy - r}L${cx + r},${cy}L${cx},${cy + r}L${cx - r},${cy}z`;
    case "cross":
      return `M${cx - r},${cy - r}L${cx + r},${cy + r}M${cx + r},${cy - r}L${cx - r},${cy + r}`;
    case "plus":
      return `M${cx - r},${cy}h${2 * r}M${cx},${cy - r}v${2 * r}`;
    default:
      return `M${cx - r},${cy}a${r},${r} 0 1,0 ${2 * r},0a${r},${r} 0 1,0 ${-2 * r},0z`;
  }
}

const OPEN = new Set<MarkerShape>(["cross", "plus"]);

function markerShape(shape: MarkerShape) {
  return function Marker(props: { cx?: number; cy?: number }) {
    const { cx, cy } = props;
    if (typeof cx !== "number" || typeof cy !== "number") return <g />;
    return (
      <path
        d={markerPath(shape, cx, cy)}
        className="ac-marker"
        fill={OPEN.has(shape) ? "none" : "currentColor"}
        stroke="currentColor"
        strokeWidth={1.5}
      />
    );
  };
}

const seriesClass = (s: SeriesModel) =>
  `ac-s ac-s${s.index % 8}${s.emphasis ? " ac-emph" : ""}`;

function Legend({ series }: { series: SeriesModel[] }) {
  const { t } = useLocale();
  if (series.length < 1) return null;
  return (
    <ul className="ac-legend">
      {series.map((s) => (
        <li key={s.key} className={seriesClass(s)}>
          <svg width="16" height="12" aria-hidden="true">
            <path
              d={markerPath(s.marker, 8, 6, 4)}
              fill={OPEN.has(s.marker) ? "none" : "currentColor"}
              stroke="currentColor"
              strokeWidth={1.5}
            />
          </svg>
          <span>
            {s.name}
            {s.unavailable
              ? ` (${t("automatic.campaign.fig.unavailable")})`
              : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

function axisTicks(cats: string[] | null) {
  return cats
    ? {
        ticks: cats.map((_, i) => i),
        tickFormatter: (v: number) => cats[Math.round(v)] ?? "",
      }
    : {};
}

const tickProps = { tick: { fontSize: 12 }, tickLine: false } as const;

function BarPlot({ plot, width }: { plot: PlotModel; width: number }) {
  const stack = plot.style === "stack";
  return (
    <BarChart
      width={width}
      height={CHART_H}
      data={plot.rows}
      margin={{ top: 18, right: 8, bottom: 4, left: 0 }}
      barGap={2}
    >
      <CartesianGrid vertical={false} />
      <XAxis dataKey="name" interval={0} {...tickProps} />
      <YAxis
        {...tickProps}
        width={46}
        domain={plot.yDomain}
        tickFormatter={(v: number) => formatValue(v, plot.yFmt)}
      />
      {plot.series
        .filter((s) => !s.unavailable)
        .map((s) => (
          <Bar
            key={s.key}
            dataKey={s.key}
            className={seriesClass(s)}
            fill="currentColor"
            stackId={stack ? "stack" : undefined}
            isAnimationActive={false}
          >
            {!stack && (
              <ErrorBar dataKey={`${s.key}_err`} width={5} strokeWidth={1.5} />
            )}
            {!stack && <LabelList dataKey={`${s.key}_label`} position="top" />}
          </Bar>
        ))}
    </BarChart>
  );
}

function LinePlot({ plot, width }: { plot: PlotModel; width: number }) {
  const step = plot.style === "step";
  return (
    <ComposedChart
      width={width}
      height={CHART_H}
      margin={{ top: 12, right: 12, bottom: 4, left: 0 }}
    >
      <CartesianGrid />
      <XAxis
        type="number"
        dataKey="x"
        domain={plot.xDomain}
        {...tickProps}
        {...axisTicks(plot.xCategories)}
        {...(plot.xCategories
          ? {}
          : {
              tickFormatter: (v: number) => formatValue(v, plot.xFmt),
            })}
      />
      <YAxis
        type="number"
        domain={plot.yDomain}
        {...tickProps}
        width={46}
        tickFormatter={(v: number) => formatValue(v, plot.yFmt)}
      />
      {plot.refs.map((r, i) =>
        r.axis === "x" ? (
          <ReferenceLine key={i} x={r.value} className="ac-ref" />
        ) : (
          <ReferenceLine key={i} y={r.value} className="ac-ref" />
        ),
      )}
      {plot.series
        .filter((s) => !s.unavailable)
        .map((s) => (
          <Line
            key={s.key}
            data={s.points}
            dataKey="y"
            type={step ? "stepAfter" : "linear"}
            className={seriesClass(s)}
            stroke="currentColor"
            strokeWidth={s.emphasis ? 3 : 1.75}
            strokeDasharray={s.dash || undefined}
            dot={step ? false : markerShape(s.marker)}
            activeDot={false}
            isAnimationActive={false}
          >
            <ErrorBar
              dataKey="err"
              direction="y"
              width={4}
              strokeWidth={1.25}
            />
          </Line>
        ))}
    </ComposedChart>
  );
}

function ForestPlot({ plot, width }: { plot: PlotModel; width: number }) {
  const rows = plot.yCategories?.length ?? 1;
  const longest = Math.max(0, ...(plot.yCategories ?? []).map((c) => c.length));
  return (
    <ScatterChart
      width={width}
      height={Math.max(CHART_H * 0.7, 56 + rows * 34)}
      margin={{ top: 8, right: 16, bottom: 4, left: 0 }}
    >
      <CartesianGrid />
      <XAxis
        type="number"
        dataKey="x"
        domain={plot.xDomain}
        {...tickProps}
        tickFormatter={(v: number) => formatValue(v, plot.xFmt)}
      />
      <YAxis
        type="number"
        dataKey="y"
        reversed
        domain={[-0.5, rows - 0.5]}
        {...tickProps}
        width={Math.min(190, 24 + longest * 6.5)}
        {...axisTicks(plot.yCategories)}
      />
      {plot.refs.map((r, i) =>
        r.axis === "x" ? (
          <ReferenceLine key={i} x={r.value} className="ac-ref" />
        ) : (
          <ReferenceLine key={i} y={r.value} className="ac-ref" />
        ),
      )}
      {plot.series
        .filter((s) => !s.unavailable)
        .map((s) => (
          <Scatter
            key={s.key}
            data={s.points}
            className={seriesClass(s)}
            fill="currentColor"
            shape={markerShape(s.marker)}
            isAnimationActive={false}
          >
            <ErrorBar dataKey="err" direction="x" width={4} strokeWidth={1.5} />
          </Scatter>
        ))}
    </ScatterChart>
  );
}

function HeatTable({ plot }: { plot: PlotModel }) {
  const { t } = useLocale();
  const heat = plot.heat;
  if (!heat) return null;
  return (
    <div
      className="ac-table-scroll"
      tabIndex={0}
      role="region"
      aria-label={plot.title || plot.yLabel}
    >
      <table className="ac-heat">
        <caption>{plot.title}</caption>
        <thead>
          <tr>
            <th scope="col">{plot.yLabel}</th>
            {heat.columns.map((c) => (
              <th scope="col" key={c}>
                {plot.xLabel ? `${plot.xLabel}: ${c}` : c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {heat.rows.map((row) => (
            <tr key={row.name}>
              <th scope="row">{row.name}</th>
              {row.cells.map((cell, i) => (
                <td
                  key={i}
                  className="ac-heat__cell"
                  style={{
                    ["--ac-share" as string]: `${Math.round(cell.share * 100)}%`,
                  }}
                >
                  <strong>{cell.count}</strong>
                  <span>{formatValue(cell.share, "percent")}</span>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="pg-pool-hint">{t("automatic.campaign.fig.heat_note")}</p>
    </div>
  );
}

function TableView({ caption, table }: { caption: string; table: TableModel }) {
  const { t } = useLocale();
  if (!table.head.length) return null;
  return (
    <details className="ac-fig-table">
      <summary>{t("Table view")}</summary>
      <div
        className="ac-table-scroll"
        tabIndex={0}
        role="region"
        aria-label={caption}
      >
        <table className="ac-data">
          <caption>{caption}</caption>
          <thead>
            <tr>
              {table.head.map((h, i) => (
                <th scope="col" key={i}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) =>
                  j === 0 ? (
                    <th scope="row" key={j}>
                      {cell}
                    </th>
                  ) : (
                    <td key={j}>{cell}</td>
                  ),
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function Plot({ plot }: { plot: PlotModel }) {
  const { ref, width } = useWidth();
  const drawn = (
    plot.style === "bars" || plot.style === "stack" ? (
      <BarPlot plot={plot} width={width} />
    ) : plot.style === "forest" ? (
      <ForestPlot plot={plot} width={width} />
    ) : (
      <LinePlot plot={plot} width={width} />
    )
  ) as ReactElement;
  return (
    <div className="ac-plot-block">
      {plot.title && <h4 className="ac-plot-title">{plot.title}</h4>}
      {plot.yLabel && plot.style !== "forest" && (
        <p className="ac-axis-label">{plot.yLabel}</p>
      )}
      <div className="ac-plot" ref={ref} aria-hidden="true">
        {drawn}
      </div>
      {plot.xLabel && plot.style !== "forest" && (
        <p className="ac-axis-label ac-axis-label--x">{plot.xLabel}</p>
      )}
      {plot.style === "forest" && plot.xLabel && (
        <p className="ac-axis-label ac-axis-label--x">{plot.xLabel}</p>
      )}
    </div>
  );
}

/** One figure: title, the summary in words, the plot(s), the legend, notes and
 * the table with every number. */
export function FigureCard({ spec }: { spec: FigureSpec }) {
  const { t, language } = useLocale();
  const model = useMemo(
    () => figureModel(spec, language, t),
    // `t` changes identity with the language only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [spec, language],
  );
  const legendSeries = useMemo(() => {
    const seen = new Map<string, SeriesModel>();
    for (const p of model.plots)
      for (const s of p.series) if (!seen.has(s.name)) seen.set(s.name, s);
    return [...seen.values()];
  }, [model]);
  const heat = model.plots.every((p) => p.style === "heat");
  return (
    <figure className="ac-figure" data-figure={model.id} data-kind={model.kind}>
      <figcaption>{model.title}</figcaption>
      {model.summary && <p className="ac-fig-summary">{model.summary}</p>}
      <div className="ac-plots">
        {model.plots.map((plot, i) =>
          plot.style === "heat" ? (
            <HeatTable plot={plot} key={i} />
          ) : (
            <Plot plot={plot} key={i} />
          ),
        )}
      </div>
      {!heat && <Legend series={legendSeries} />}
      {model.notes.length > 0 && (
        <ul className="ac-fig-notes">
          {model.notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ul>
      )}
      {model.plots.map((plot, i) => (
        <TableView
          key={i}
          caption={plot.title ? `${model.title} · ${plot.title}` : model.title}
          table={plot.style === "heat" ? { head: [], rows: [] } : plot.table}
        />
      ))}
    </figure>
  );
}
