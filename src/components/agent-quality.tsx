"use client";
import { useEffect, useState } from "react";
import { T, useLocale } from "./levi-locale";
type Report = {
  episode: number;
  uncovered_intervals: [number, number][];
  warnings: { proposal: number; reason: string; boundary?: number }[];
};
export default function AgentQuality({
  runId,
  revision,
}: {
  runId: string;
  revision: number;
}) {
  const [reports, setReports] = useState<Report[]>([]),
    [error, setError] = useState(false);
  const { t } = useLocale();
  useEffect(() => {
    let cancelled = false;
    setReports([]);
    setError(false);
    void fetch("/api/levi/agent/v1/tools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "runs.quality",
        arguments: { run_id: runId },
      }),
    })
      .then(async (r) => {
        if (!r.ok) throw new Error("quality");
        return r.json();
      })
      .then((r) => {
        if (!cancelled) setReports(r.episodes || []);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, revision]);
  return (
    <T>
      <details className="levi-review-queue" open>
        <summary>Coverage and uncertainty</summary>
        <p>
          Uncovered time is not evidence of inactivity. Review short events
          between samples.
        </p>
        {error && (
          <p role="alert">
            Quality report unavailable; validate before approval.
          </p>
        )}
        {reports.map((row) => (
          <div key={row.episode}>
            <strong>
              <T>Episode</T> {row.episode}
            </strong>
            <p>
              <T>Uncovered intervals</T>:{" "}
              {row.uncovered_intervals
                .map(([a, b]) => `${a.toFixed(3)}–${b.toFixed(3)} s`)
                .join(", ") || t("None reported")}
            </p>
            <ul>
              {row.warnings.map((w, i) => (
                <li key={i}>
                  #{w.proposal + 1} · {t(w.reason)}{" "}
                  {w.boundary !== undefined ? `${w.boundary.toFixed(3)} s` : ""}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </details>
    </T>
  );
}
