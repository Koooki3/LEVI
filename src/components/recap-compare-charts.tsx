"use client";

/** Comparison charts for two saved RECAP results (A and B): label agreement
 * by outcome, mean Value by outcome, Value distributions, |B − A| per frame
 * and per-episode differences. Each chart has a text summary and a table
 * view with the exact numbers (the chart itself is hidden from assistive
 * technology); colours come from design tokens through CSS classes, and A/B
 * are always named in text, never by colour alone. */
import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ReferenceLine,
  XAxis,
  YAxis,
} from "recharts";
import { useLocale } from "@/components/levi-locale";
import type { RecapComparison } from "@/types/recap.types";
import {
  absDiffHistogram,
  agreementBars,
  chartNumber,
  episodeDiffBars,
  outcomeKey,
  outcomeMeanBars,
  valueHistogram,
} from "@/components/recap-compare-data";

const CHART_H = 180;
const FALLBACK_W = 360;

/** The plot's width from its container. Recharts' ResponsiveContainer draws
 * nothing until it has measured a size (it never does in a test DOM), so the
 * chart starts at a fallback width and follows the container from there. */
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

function Plot({
  summary,
  children,
}: {
  summary: string;
  children: (width: number) => React.ReactElement;
}) {
  const { ref, width } = useWidth();
  return (
    <>
      <p className="recap-chart-summary">{summary}</p>
      <div className="recap-chart-plot" ref={ref} aria-hidden="true">
        {children(width)}
      </div>
    </>
  );
}

function Legend({ a, b }: { a: string; b: string }) {
  return (
    <span className="recap-chart-legend">
      <span>
        <span className="recap-chart-swatch side-a" aria-hidden="true" />A · {a}
      </span>
      <span>
        <span className="recap-chart-swatch side-b" aria-hidden="true" />B · {b}
      </span>
    </span>
  );
}

function TableView({
  caption,
  head,
  rows,
}: {
  caption: string;
  head: string[];
  rows: (string | number)[][];
}) {
  const { t } = useLocale();
  return (
    <details className="recap-chart-table">
      <summary>{t("Table view")}</summary>
      <div
        className="recap-table-scroll"
        tabIndex={0}
        role="region"
        aria-label={caption}
      >
        <table className="recap-comparison-table">
          <caption>{caption}</caption>
          <thead>
            <tr>
              {head.map((cell, i) => (
                <th scope="col" key={i}>
                  {cell}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
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

const axisProps = {
  tick: { fontSize: 12 },
  tickLine: false,
} as const;
const pct = (value: number) => chartNumber(value, 1) + "%";
const range = (lo: number, hi: number, digits = 3) =>
  chartNumber(lo, digits) + "…" + chartNumber(hi, digits);

/** Memoised: the section re-renders with the playhead (about four times a
 * second during playback), but the charts depend only on the comparison,
 * the episode and the language. */
export const RecapComparisonCharts = React.memo(function RecapComparisonCharts({
  data,
  episodeId,
}: {
  data: RecapComparison;
  episodeId: number;
}) {
  const { t } = useLocale();
  const nameA = data.a.checkpoint || data.a.revision_id;
  const nameB = data.b.checkpoint || data.b.revision_id;
  const groupName = (group: string) =>
    group === "all" ? t("All shared frames") : t(outcomeKey(group));
  const { agreement, means, histogram, absDiff } = useMemo(
    () => ({
      agreement: agreementBars(data),
      means: outcomeMeanBars(data),
      histogram: valueHistogram(data),
      absDiff: absDiffHistogram(data),
    }),
    [data],
  );
  const episodes = useMemo(
    () => episodeDiffBars(data, episodeId),
    [data, episodeId],
  );
  const figures: React.ReactNode[] = [];

  if (agreement) {
    const caption = t("Label agreement and positive frames by outcome");
    figures.push(
      <figure className="recap-chart" key="agreement">
        <figcaption>{caption}</figcaption>
        <Legend a={nameA} b={nameB} />
        <Plot
          summary={agreement
            .map(
              (bar) =>
                groupName(bar.group) +
                ": " +
                t("Label agreement") +
                " " +
                pct(bar.agreement) +
                ", " +
                t("positive frames") +
                " A " +
                pct(bar.positiveA) +
                " / B " +
                pct(bar.positiveB),
            )
            .join(" · ")}
        >
          {(width) => (
            <BarChart
              width={width}
              height={CHART_H}
              data={agreement.map((bar) => ({
                ...bar,
                name: groupName(bar.group),
              }))}
              margin={{ top: 16, right: 8, bottom: 0, left: 0 }}
              barGap={2}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="name" {...axisProps} interval={0} />
              <YAxis
                {...axisProps}
                domain={[0, 100]}
                width={36}
                tickFormatter={(v: number) => v + "%"}
              />
              <Bar
                dataKey="agreement"
                className="recap-bar-agree"
                fill="currentColor"
                isAnimationActive={false}
              >
                <LabelList
                  dataKey="agreement"
                  position="top"
                  formatter={(v: number) => pct(v)}
                />
              </Bar>
              <Bar
                dataKey="positiveA"
                className="recap-bar-a"
                fill="currentColor"
                isAnimationActive={false}
              />
              <Bar
                dataKey="positiveB"
                className="recap-bar-b"
                fill="currentColor"
                isAnimationActive={false}
              />
            </BarChart>
          )}
        </Plot>
        <span className="recap-chart-legend">
          <span>
            <span className="recap-chart-swatch agree" aria-hidden="true" />
            {t("Label agreement")}
          </span>
          <span>{t("then positive frames of A and of B")}</span>
        </span>
        <TableView
          caption={caption}
          head={[
            t("Group"),
            t("frames"),
            t("Label agreement"),
            t("positive frames") + " A",
            t("positive frames") + " B",
          ]}
          rows={agreement.map((bar) => [
            groupName(bar.group),
            bar.frames,
            pct(bar.agreement),
            pct(bar.positiveA),
            pct(bar.positiveB),
          ])}
        />
      </figure>,
    );
  }

  if (means.length) {
    const caption = t("Mean Value by outcome");
    figures.push(
      <figure className="recap-chart" key="means">
        <figcaption>{caption}</figcaption>
        <Legend a={nameA} b={nameB} />
        <Plot
          summary={means
            .map(
              (bar) =>
                t(outcomeKey(bar.outcome)) +
                " (" +
                bar.episodes +
                "): A " +
                chartNumber(bar.a) +
                ", B " +
                chartNumber(bar.b) +
                ", B − A " +
                chartNumber(bar.diff),
            )
            .join(" · ")}
        >
          {(width) => (
            <BarChart
              width={width}
              height={CHART_H}
              data={means.map((bar) => ({
                ...bar,
                name: t(outcomeKey(bar.outcome)),
              }))}
              margin={{ top: 8, right: 8, bottom: 16, left: 0 }}
              barGap={2}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="name" {...axisProps} interval={0} />
              <YAxis
                {...axisProps}
                width={44}
                domain={["auto", 0]}
                tickFormatter={(v: number) => chartNumber(v, 2)}
              />
              <ReferenceLine y={0} className="recap-chart-zero" />
              <Bar
                dataKey="a"
                className="recap-bar-a"
                fill="currentColor"
                isAnimationActive={false}
              >
                <LabelList
                  dataKey="a"
                  position="bottom"
                  formatter={(v: number) => chartNumber(v)}
                />
              </Bar>
              <Bar
                dataKey="b"
                className="recap-bar-b"
                fill="currentColor"
                isAnimationActive={false}
              >
                <LabelList
                  dataKey="b"
                  position="bottom"
                  formatter={(v: number) => chartNumber(v)}
                />
              </Bar>
            </BarChart>
          )}
        </Plot>
        <p className="recap-chart-note">
          {t(
            "Mean of the episodes' mean Value on shared frames. Values lie below 0: a shorter bar (closer to 0) for success than for failure means the model separates the outcomes.",
          )}
        </p>
        <TableView
          caption={caption}
          head={[t("Group"), t("episodes"), "A", "B", "B − A"]}
          rows={means.map((bar) => [
            t(outcomeKey(bar.outcome)),
            bar.episodes,
            chartNumber(bar.a, 4),
            chartNumber(bar.b, 4),
            chartNumber(bar.diff, 4),
          ])}
        />
      </figure>,
    );
  }

  if (histogram) {
    const caption = t("Value distribution");
    const unit = t(histogram.unit === "frames" ? "frames" : "episodes");
    const peak = (side: "a" | "b") =>
      histogram.rows.reduce(
        (best, row) => (row[side] > best[side] ? row : best),
        histogram.rows[0],
      );
    figures.push(
      <figure className="recap-chart" key="histogram">
        <figcaption>
          {caption} ·{" "}
          {t(
            histogram.unit === "frames"
              ? "per shared frame"
              : "per episode (mean Value)",
          )}
        </figcaption>
        <Legend a={nameA} b={nameB} />
        <Plot
          summary={
            t("Most common Value") +
            ": A " +
            range(peak("a").lo, peak("a").hi) +
            ", B " +
            range(peak("b").lo, peak("b").hi)
          }
        >
          {(width) => (
            <BarChart
              width={width}
              height={CHART_H}
              data={histogram.rows.map((row) => ({
                ...row,
                name: chartNumber((row.lo + row.hi) / 2, 2),
              }))}
              margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
              barGap={0}
              barCategoryGap={1}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="name" {...axisProps} minTickGap={12} />
              <YAxis {...axisProps} width={40} allowDecimals={false} />
              <Bar
                dataKey="a"
                className="recap-bar-a"
                fill="currentColor"
                isAnimationActive={false}
              />
              <Bar
                dataKey="b"
                className="recap-bar-b"
                fill="currentColor"
                isAnimationActive={false}
              />
            </BarChart>
          )}
        </Plot>
        <TableView
          caption={caption}
          head={[t("Value bin"), unit + " A", unit + " B"]}
          rows={histogram.rows.map((row) => [
            range(row.lo, row.hi),
            row.a,
            row.b,
          ])}
        />
      </figure>,
    );
  }

  if (absDiff) {
    const caption = t("Per-frame |B − A| distribution");
    const total = absDiff.reduce((sum, row) => sum + row.count, 0);
    figures.push(
      <figure className="recap-chart" key="absdiff">
        <figcaption>{caption}</figcaption>
        <Plot
          summary={
            t("Mean absolute difference") +
            " " +
            chartNumber(data.value?.mean_abs_diff ?? NaN, 4) +
            " · " +
            total +
            " " +
            t("shared frames")
          }
        >
          {(width) => (
            <BarChart
              width={width}
              height={CHART_H * 0.8}
              data={absDiff.map((row) => ({
                ...row,
                name: chartNumber((row.lo + row.hi) / 2, 3),
              }))}
              margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
              barCategoryGap={1}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="name" {...axisProps} minTickGap={12} />
              <YAxis {...axisProps} width={40} allowDecimals={false} />
              <Bar
                dataKey="count"
                className="recap-bar-diff"
                fill="currentColor"
                isAnimationActive={false}
              />
            </BarChart>
          )}
        </Plot>
        <TableView
          caption={caption}
          head={["|B − A|", t("frames")]}
          rows={absDiff.map((row) => [range(row.lo, row.hi, 4), row.count])}
        />
      </figure>,
    );
  }

  if (episodes.rows.length) {
    const caption = t("Per-episode mean Value difference (B − A)");
    figures.push(
      <figure className="recap-chart" key="episodes">
        <figcaption>
          {caption}
          {episodes.total > episodes.rows.length &&
            " · " +
              episodes.rows.length +
              " / " +
              episodes.total +
              " " +
              t("episodes with the largest differences")}
        </figcaption>
        <Plot
          summary={t(
            "Of {total} episodes: B higher in {b}, A higher in {a}, equal in {same}.",
          )
            .replace("{total}", String(episodes.total))
            .replace("{b}", String(episodes.counts.higherB))
            .replace("{a}", String(episodes.counts.higherA))
            .replace("{same}", String(episodes.counts.equal))}
        >
          {(width) => (
            <BarChart
              width={width}
              height={CHART_H}
              data={episodes.rows.map((row) => ({
                ...row,
                name: String(row.episode),
              }))}
              margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
              barCategoryGap={1}
            >
              <CartesianGrid vertical={false} />
              <XAxis dataKey="name" {...axisProps} minTickGap={8} />
              <YAxis
                {...axisProps}
                width={44}
                tickFormatter={(v: number) => chartNumber(v, 2)}
              />
              <ReferenceLine y={0} className="recap-chart-zero" />
              <Bar
                dataKey="diff"
                className="recap-bar-episodes"
                fill="currentColor"
                isAnimationActive={false}
              >
                {episodes.rows.map((row) => (
                  <Cell
                    key={row.episode}
                    className={
                      (row.diff >= 0 ? "higher-b" : "higher-a") +
                      (row.current ? " current" : "")
                    }
                  />
                ))}
              </Bar>
            </BarChart>
          )}
        </Plot>
        <p className="recap-chart-note">
          {t(
            "Bars above zero: B's mean Value is higher on that episode; below zero: A's. The current episode is outlined.",
          )}
        </p>
        <TableView
          caption={caption}
          head={[t("Episode"), t("Outcome"), "B − A"]}
          rows={episodes.rows.map((row) => [
            row.episode + (row.current ? " · " + t("current") : ""),
            row.outcome ? t(outcomeKey(row.outcome)) : "—",
            chartNumber(row.diff, 4),
          ])}
        />
      </figure>,
    );
  }

  if (!figures.length) return null;
  return (
    <section className="recap-charts" aria-label={t("Comparison charts")}>
      {figures}
    </section>
  );
});
