"use client";
// The metrics of a run: rates with their Wilson 95 % interval and the groups
// that mean the same in both reset modes. With 20 to 30 episodes the interval,
// not the rate, is the result. The agreement group is left out while the last
// episode is unlabelled.
import { Table } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { metricRows } from "./run-logic";
import type { MetricsReport } from "./types";

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
  return (
    <>
      <p className="ar-muted">{t("automatic.run.metrics.hint")}</p>
      <Table density="compact" caption={t("automatic.run.metrics.title")}>
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
                {row.interval
                  ? `${(row.interval[0] * 100).toFixed(1)} – ${(row.interval[1] * 100).toFixed(1)} %`
                  : "—"}
              </td>
              <td>
                {row.comparable === null
                  ? "—"
                  : row.comparable
                    ? t("automatic.run.metrics.comparable.yes")
                    : t("automatic.run.metrics.comparable.no")}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
    </>
  );
}
