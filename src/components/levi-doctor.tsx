"use client";
import { useState } from "react";
import { T, useLocale } from "./levi-locale";
import { leviApi, downloadJson, exportName } from "./levi-api";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import { RawCaptureNotice } from "@/components/raw-capture-notice";
const CHECKS = [
  "metadata",
  "temporal",
  "action",
  "video",
  "distribution",
  "episodes",
  "features",
  "training",
  "anomaly",
  "portability",
];
type Report = {
  episodes: number;
  frames: number;
  fps: number;
  status: string;
  scope: string;
  counts: Record<string, number>;
  results: {
    check: string;
    status: string;
    message: string;
    episode: number | null;
  }[];
  flagged_episodes: number[];
  method: string;
};

export default function LeviDoctor({ repoId }: { repoId: string }) {
  const [checks, setChecks] = useState(CHECKS);
  // A local dataset is checked in full, with video decoded, by default, so a
  // PASS here means what quality.inspect's PASS means. Hub datasets keep a
  // sample: analysing them downloads data.
  const local = repoId.startsWith("local/");
  const [max, setMax] = useState(local ? 0 : 20);
  const [decode, setDecode] = useState(local);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [raw, setRaw] = useState(false);
  const { addMany } = useFlaggedEpisodes();
  const { t } = useLocale();
  async function run() {
    setBusy(true);
    setError("");
    try {
      setReport(
        await leviApi<Report>("diagnostics", {
          repo_id: repoId,
          max_episodes: max,
          checks,
          decode_video: decode,
        }),
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="levi-diagnostic">
      <span className="levi-eyebrow">
        <T>LEVI / DATASET DOCTOR</T>
      </span>
      <h2>
        <T>Dataset quality diagnostics</T>
      </h2>
      <p className="mt-3 text-sm text-slate-400">
        <T>
          Version-aware local checks for timestamps, actions, videos and
          metadata. Reports identify review candidates; thresholds are
          configurable heuristics.
        </T>
      </p>
      <div className="mt-4">
        <RawCaptureNotice feature="doctor" />
      </div>
      <div className="levi-box">
        <div className="levi-row">
          <code className="grow text-xs">{repoId}</code>
          <label className="text-xs">
            <T>Max episodes (0 = all)</T>{" "}
            <input
              aria-label={t("Max episodes (0 = all)")}
              className="levi-input w-24"
              type="number"
              min="0"
              max="10000"
              value={max}
              onChange={(e) => setMax(Number(e.target.value))}
            />
          </label>
        </div>
        <div className="levi-checks">
          {CHECKS.map((c) => (
            <label key={c}>
              <input
                type="checkbox"
                checked={checks.includes(c)}
                onChange={(e) =>
                  setChecks((prev) =>
                    e.target.checked
                      ? [...prev, c]
                      : prev.filter((x) => x !== c),
                  )
                }
              />
              {t("check." + c)}
            </label>
          ))}
        </div>
        <label className="text-xs flex gap-2 mb-4">
          <input
            type="checkbox"
            checked={decode}
            onChange={(e) => setDecode(e.target.checked)}
          />
          <T>Decode video samples (downloads remote videos)</T>
        </label>
        <p className="text-xs text-slate-400 mb-4">
          <T>
            Remote analysis downloads parquet shards. The episode limit applies
            to analysis, not download size.
          </T>
        </p>
        <button
          className="levi-primary"
          onClick={run}
          disabled={busy || !checks.length}
        >
          <T>{busy ? "Running diagnostics…" : "Run diagnostics"}</T>
        </button>
        {!repoId.startsWith("local/") && (
          <a
            className="ml-5 text-xs underline text-slate-400"
            target="_blank"
            rel="noreferrer"
            href={`https://jashshah999-lerobot-doctor.hf.space/?dataset=${repoId}`}
          >
            <T>Open upstream lerobot-doctor</T> ↗
          </a>
        )}
      </div>
      {error && (
        <p role="alert" className="levi-error">
          {error}
        </p>
      )}
      {report && (
        <>
          <div className="levi-row">
            <span className={`levi-status ${report.status}`}>
              {report.status.toUpperCase()}
            </span>
            <span className="text-xs">
              <T>Scope</T>: <T>{report.scope}</T>
            </span>
            <button className="levi-secondary" onClick={() => setRaw(!raw)}>
              {raw ? t("Report") : "JSON"}
            </button>
            <button
              className="levi-secondary"
              onClick={() =>
                downloadJson(report, exportName(repoId, "quality"))
              }
            >
              <T>Export report</T>
            </button>
            <button
              className="levi-secondary"
              onClick={() => addMany(report.flagged_episodes)}
            >
              <T>Flag findings for review</T> ({report.flagged_episodes.length})
            </button>
          </div>
          <div className="levi-metrics">
            {[
              ["Episodes", report.episodes],
              ["Frames", report.frames],
              ["Pass", report.counts.pass],
              ["Warn", report.counts.warn],
              ["Fail", report.counts.fail],
            ].map(([k, v]) => (
              <div key={k}>
                <span className="text-xs text-slate-400">
                  <T>{k}</T>
                </span>
                <strong>{v}</strong>
              </div>
            ))}
          </div>
          {raw ? (
            <pre className="levi-code">{JSON.stringify(report, null, 2)}</pre>
          ) : (
            CHECKS.filter((c) => report.results.some((r) => r.check === c)).map(
              (c) => (
                <div className="levi-box" key={c}>
                  <h3 className="mb-4">{t("check." + c)}</h3>
                  {report.results
                    .filter((r) => r.check === c)
                    .map((r, i) => (
                      <div className="levi-row text-xs mb-3" key={i}>
                        <span className={`levi-status ${r.status}`}>
                          {r.status.toUpperCase()}
                        </span>
                        {r.episode !== null && <strong>EP {r.episode}</strong>}
                        <T>{r.message}</T>
                      </div>
                    ))}
                </div>
              ),
            )
          )}
          <p className="text-xs text-slate-400">
            <T>{report.method}</T>
          </p>
        </>
      )}
    </section>
  );
}
