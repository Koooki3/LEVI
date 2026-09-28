"use client";
// Components for the report's fenced blocks (src/utils/report.ts).
import { createContext, useContext, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useLocale } from "@/components/levi-locale";
import {
  type BlockSpec,
  type Metric,
  type ReportLang,
  type ReportStatus,
  type ReportBlockLanguage,
  type SortDirection,
  cellText,
  chartSeries,
  clampProgress,
  deltaTone,
  formatAge,
  formatDelta,
  formatEta,
  formatNumber,
  lookupChart,
  lookupMetrics,
  lookupMilestones,
  lookupTable,
  lookupWorkstreams,
  normalizeState,
  parseBlock,
  pick,
  sortRows,
  sortWorkstreams,
} from "@/utils/report";

export interface ReportContextValue {
  status: ReportStatus | null;
  lang: ReportLang;
  now: number;
}

export const ReportContext = createContext<ReportContextValue>({
  status: null,
  lang: "en",
  now: 0,
});

/** Categorical series colours, validated for the dark report surface. */
export const SERIES_COLORS = [
  "#7fa22c",
  "#3987e5",
  "#d95926",
  "#9085e9",
  "#c98500",
  "#d55181",
];

export function BlockError({ message }: { message: string }) {
  const { t } = useLocale();
  return (
    <div className="lr-block-error" role="alert">
      <strong>{t("report.blockError")}</strong> {message}
    </div>
  );
}

export function ReportBlock({
  language,
  source,
}: {
  language: ReportBlockLanguage;
  source: string;
}) {
  const parsed = parseBlock(language, source);
  if (!parsed.ok)
    return <BlockError message={`${language}: ${parsed.error}`} />;
  return <SpecBlock spec={parsed.spec} />;
}

function SpecBlock({ spec }: { spec: BlockSpec }) {
  switch (spec.kind) {
    case "progress":
      return <ProgressBlock id={spec.id} />;
    case "chart":
      return <ChartBlock spec={spec} />;
    case "metrics":
      return <MetricsBlock data={spec.data} />;
    case "table":
      return <TableBlock data={spec.data} />;
    case "timeline":
      return <TimelineBlock data={spec.data} />;
  }
}

// ------------------------------------------------------------ progress

function ProgressBlock({ id }: { id?: string }) {
  const { status, lang, now } = useContext(ReportContext);
  const { t } = useLocale();
  const found = lookupWorkstreams(status, { id });
  if (!found.ok) return <BlockError message={found.error} />;
  if (!found.value.length)
    return <p className="lr-muted">{t("report.noWorkstreams")}</p>;
  return (
    <div className="lr-progress-list">
      {sortWorkstreams(found.value).map((item) => {
        const state = normalizeState(item.state);
        const progress = clampProgress(item.progress);
        const pct = progress === null ? null : Math.round(progress * 100);
        const eta = formatEta(item.eta, now, lang);
        const age = formatAge(item.updated_at, now, lang);
        return (
          <div className={`lr-progress lr-state-${state}`} key={item.id}>
            <div className="lr-progress-head">
              <span className="lr-chip">{item.id}</span>
              <span className="lr-progress-title">
                {pick(item.title, lang) || item.id}
              </span>
              <span className={`lr-state lr-state-badge-${state}`}>
                <span className="lr-state-dot" aria-hidden="true" />
                {t(`report.state.${state}`)}
              </span>
              <span className="lr-progress-pct">
                {pct === null ? "—" : `${pct}%`}
              </span>
            </div>
            <div
              className="lr-bar"
              role="progressbar"
              aria-label={pick(item.title, lang) || item.id}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={pct ?? undefined}
            >
              <div className="lr-bar-fill" style={{ width: `${pct ?? 0}%` }} />
            </div>
            <div className="lr-progress-meta">
              {pick(item.stage, lang) && (
                <span className="lr-stage">{pick(item.stage, lang)}</span>
              )}
              {age && (
                <span title={item.updated_at ?? undefined}>
                  {t("report.updatedAgo")} {age}
                </span>
              )}
              {eta && (
                <span>
                  {t("report.eta")} {eta}
                </span>
              )}
              {item.owner && item.owner !== item.id && (
                <span>
                  {t("report.owner")} {item.owner}
                </span>
              )}
              {(item.links ?? []).map((link, index) =>
                link?.path ? (
                  <span className="lr-ref" key={index} title={link.path}>
                    {pick(link.label, lang) || link.path}
                  </span>
                ) : null,
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------ charts

function ChartTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: { name?: string; value?: unknown; color?: string }[];
  label?: unknown;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="lr-tooltip">
      <div className="lr-tooltip-label">{String(label ?? "")}</div>
      {payload.map((entry, index) => (
        <div className="lr-tooltip-row" key={index}>
          <span
            className="lr-swatch"
            style={{ background: entry.color }}
            aria-hidden="true"
          />
          <span>{entry.name}</span>
          <strong>{formatNumber(entry.value, undefined)}</strong>
        </div>
      ))}
    </div>
  );
}

function ChartBlock({ spec }: { spec: Extract<BlockSpec, { kind: "chart" }> }) {
  const { status, lang } = useContext(ReportContext);
  const found = lookupChart(status, spec.data);
  if (!found.ok) return <BlockError message={found.error} />;
  const rows = found.value.map((row) => {
    const x = row[spec.x];
    return {
      ...row,
      [spec.x]: typeof x === "object" && x ? pick(x as never, lang) : x,
    };
  });
  const series = chartSeries(rows, spec.x, spec.series).slice(
    0,
    SERIES_COLORS.length,
  );
  const title = pick(spec.title, lang);
  const yLabel = pick(spec.y_label, lang);
  const axis = { stroke: "#89927f", fontSize: 11 };
  const common = {
    data: rows,
    margin: { top: 8, right: 16, bottom: 4, left: yLabel ? 12 : 0 },
  };
  const children = [
    <CartesianGrid key="grid" stroke="#ffffff12" vertical={false} />,
    <XAxis
      key="x"
      dataKey={spec.x}
      tick={{ fill: "#afb7a8", fontSize: 11 }}
      stroke="#ffffff30"
      interval="preserveStartEnd"
    />,
    <YAxis
      key="y"
      tick={{ fill: "#afb7a8", fontSize: 11 }}
      stroke="#ffffff30"
      domain={spec.y_domain ?? ["auto", "auto"]}
      tickFormatter={(value: number) => formatNumber(value, undefined)}
      width={52}
      label={
        yLabel
          ? {
              value: yLabel,
              angle: -90,
              position: "insideLeft",
              fill: axis.stroke,
              fontSize: 11,
              style: { textAnchor: "middle" },
            }
          : undefined
      }
    />,
    <Tooltip
      key="tooltip"
      content={<ChartTooltip />}
      cursor={
        spec.type === "line" ? { stroke: "#ffffff40" } : { fill: "#ffffff0d" }
      }
    />,
    series.length > 1 ? (
      <Legend
        key="legend"
        wrapperStyle={{ fontSize: 12, color: "#afb7a8" }}
        iconType="circle"
        iconSize={8}
      />
    ) : null,
  ];
  return (
    <figure className="lr-chart">
      {title && <figcaption>{title}</figcaption>}
      {!rows.length ? (
        <p className="lr-muted">—</p>
      ) : (
        <div className="lr-chart-body">
          <ResponsiveContainer width="100%" height="100%">
            {spec.type === "line" ? (
              <LineChart {...common}>
                {children}
                {series.map((item, index) => (
                  <Line
                    key={item.key}
                    type="monotone"
                    dataKey={item.key}
                    name={pick(item.label, lang) || item.key}
                    stroke={SERIES_COLORS[index]}
                    strokeWidth={2}
                    dot={{ r: 3, strokeWidth: 0, fill: SERIES_COLORS[index] }}
                    activeDot={{ r: 5, stroke: "#1b231b", strokeWidth: 2 }}
                    connectNulls
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            ) : (
              <BarChart {...common} barGap={2} barCategoryGap="22%">
                {children}
                {series.map((item, index) => (
                  <Bar
                    key={item.key}
                    dataKey={item.key}
                    name={pick(item.label, lang) || item.key}
                    fill={SERIES_COLORS[index]}
                    radius={[4, 4, 0, 0]}
                    maxBarSize={48}
                    isAnimationActive={false}
                  />
                ))}
              </BarChart>
            )}
          </ResponsiveContainer>
        </div>
      )}
    </figure>
  );
}

// ------------------------------------------------------------ metrics

function MetricCard({ metric }: { metric: Metric }) {
  const { lang } = useContext(ReportContext);
  const { t } = useLocale();
  const tone = deltaTone(metric.value, metric.baseline, metric.better);
  const delta = formatDelta(metric.value, metric.baseline, metric.format, lang);
  const arrow =
    typeof metric.value === "number" && typeof metric.baseline === "number"
      ? metric.value > metric.baseline
        ? "▲"
        : metric.value < metric.baseline
          ? "▼"
          : "="
      : "";
  return (
    <div className="lr-metric">
      <div className="lr-metric-label">{pick(metric.label, lang)}</div>
      <div className="lr-metric-value">
        {formatNumber(metric.value, metric.format, lang)}
      </div>
      {delta && (
        <div className={`lr-delta lr-tone-${tone}`}>
          <span aria-hidden="true">{arrow}</span> {delta}
          <span className="lr-baseline">
            {" "}
            {t("report.vsBaseline")}{" "}
            {formatNumber(metric.baseline, metric.format, lang)}
          </span>
        </div>
      )}
      {pick(metric.note, lang) && (
        <div className="lr-metric-note">{pick(metric.note, lang)}</div>
      )}
    </div>
  );
}

function MetricsBlock({ data }: { data: string | Metric[] }) {
  const { status } = useContext(ReportContext);
  const found = lookupMetrics(status, data);
  if (!found.ok) return <BlockError message={found.error} />;
  return (
    <div className="lr-metrics">
      {found.value.map((metric, index) => (
        <MetricCard metric={metric} key={index} />
      ))}
    </div>
  );
}

// ------------------------------------------------------------ table

function TableBlock({ data }: { data: Parameters<typeof lookupTable>[1] }) {
  const { status, lang } = useContext(ReportContext);
  const { t } = useLocale();
  const [sort, setSort] = useState<{ key: string | null; dir: SortDirection }>({
    key: null,
    dir: "asc",
  });
  const found = lookupTable(status, data);
  if (!found.ok) return <BlockError message={found.error} />;
  const { columns, rows } = found.value;
  const sorted = sortRows(rows, sort.key, sort.dir);
  const numeric = (key: string) =>
    rows.some((row) => typeof row[key] === "number") &&
    rows.every((row) => row[key] == null || typeof row[key] === "number");
  return (
    <div className="lr-table-wrap">
      <table className="lr-table">
        <thead>
          <tr>
            {columns.map((column) => {
              const active = sort.key === column.key;
              return (
                <th
                  key={column.key}
                  aria-sort={
                    active
                      ? sort.dir === "asc"
                        ? "ascending"
                        : "descending"
                      : "none"
                  }
                  className={numeric(column.key) ? "lr-num" : undefined}
                >
                  <button
                    type="button"
                    title={t("report.sort")}
                    onClick={() =>
                      setSort(
                        active && sort.dir === "asc"
                          ? { key: column.key, dir: "desc" }
                          : active
                            ? { key: null, dir: "asc" }
                            : { key: column.key, dir: "asc" },
                      )
                    }
                  >
                    {pick(column.label, lang) || column.key}
                    <span className="lr-sort" aria-hidden="true">
                      {active ? (sort.dir === "asc" ? "↑" : "↓") : "↕"}
                    </span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={numeric(column.key) ? "lr-num" : undefined}
                >
                  {cellText(row[column.key], lang)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------ timeline

function TimelineBlock({
  data,
}: {
  data: Parameters<typeof lookupMilestones>[1];
}) {
  const { status, lang } = useContext(ReportContext);
  const { t } = useLocale();
  const found = lookupMilestones(status, data);
  if (!found.ok) return <BlockError message={found.error} />;
  return (
    <ol className="lr-timeline">
      {found.value.map((item, index) => {
        const state =
          item.state === "done" || item.state === "current"
            ? item.state
            : "next";
        return (
          <li className={`lr-milestone lr-milestone-${state}`} key={index}>
            <span className="lr-milestone-dot" aria-hidden="true" />
            <span className="lr-milestone-date">
              {/^\d{4}-\d{2}-\d{2}/.test(item.date ?? "") ? (
                <>
                  {item.date!.slice(5, 10)}
                  <span className="lr-milestone-year">
                    {item.date!.slice(0, 4)}
                  </span>
                </>
              ) : (
                item.date
              )}
            </span>
            <span className="lr-milestone-title">{pick(item.title, lang)}</span>
            <span className="lr-milestone-state">
              {t(`report.milestone.${state}`)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
