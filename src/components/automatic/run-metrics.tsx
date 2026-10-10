"use client";
// The metrics of a run: rates with their Wilson 95 % interval and the groups
// that mean the same in both reset modes. With 20 to 30 episodes the interval,
// not the rate, is the result. The agreement group is left out while the last
// episode is unlabelled. The page shows the metrics in groups with plain names;
// the report's own keys stay one click away under "All fields".
import { Table } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import {
  groupName,
  metricGroups,
  metricName,
  metricRows,
  metricValue,
  type MetricRow,
} from "./run-logic";
import type { MetricsReport } from "./types";

function Interval({ row }: { row: MetricRow }) {
  return row.interval && !row.noData
    ? `${(row.interval[0] * 100).toFixed(1)} – ${(row.interval[1] * 100).toFixed(1)} %`
    : "—";
}

export function RunMetrics({
  report,
  blind,
}: {
  report: MetricsReport;
  blind: boolean;
}) {
  const { t } = useLocale();
  const rows = metricRows(report, blind);
  if (rows.length === 0)
    return <p className="ar-muted">{t("automatic.run.metrics.empty")}</p>;
  const comparableText = (row: MetricRow) =>
    row.comparable === null
      ? "—"
      : row.comparable
        ? t("automatic.run.metrics.comparable.yes")
        : t("automatic.run.metrics.comparable.no");
  return (
    <>
      <p className="ar-muted">{t("automatic.run.metrics.hint")}</p>
      {metricGroups(rows).map((group) => (
        <section key={group.id} className="ar-metric-group">
          <h3>{groupName(group.id, t)}</h3>
          <Table density="compact" caption={groupName(group.id, t)}>
            <thead>
              <tr>
                <th scope="col">{t("automatic.run.metrics.name")}</th>
                <th scope="col">{t("automatic.run.metrics.value")}</th>
                <th scope="col">{t("automatic.run.metrics.interval")}</th>
                <th scope="col">{t("automatic.run.metrics.comparable")}</th>
              </tr>
            </thead>
            <tbody>
              {group.rows.map((row) => (
                <tr key={row.path}>
                  <td>{metricName(row.path, t)}</td>
                  <td className="ds-num">{metricValue(row, t)}</td>
                  <td className="ds-num">
                    <Interval row={row} />
                  </td>
                  <td>{comparableText(row)}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </section>
      ))}
      <details className="ar-raw">
        <summary>{t("automatic.metric.all_fields")}</summary>
        <Table density="compact" caption={t("automatic.metric.all_fields")}>
          <thead>
            <tr>
              <th scope="col">{t("automatic.run.metrics.name")}</th>
              <th scope="col">{t("automatic.run.metrics.value")}</th>
              <th scope="col">{t("automatic.run.metrics.interval")}</th>
              <th scope="col">{t("automatic.run.metrics.comparable")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.path}>
                <td>
                  <code>{row.path}</code>
                </td>
                <td className="ds-num">{row.value}</td>
                <td className="ds-num">
                  <Interval row={row} />
                </td>
                <td>{comparableText(row)}</td>
              </tr>
            ))}
          </tbody>
        </Table>
      </details>
    </>
  );
}
