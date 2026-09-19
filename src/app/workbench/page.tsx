"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { T, useLocale } from "@/components/levi-locale";
import { leviApi } from "@/components/levi-api";
import { ConversionWizard } from "@/components/conversion/conversion-wizard";
import { FormatsTable } from "@/components/conversion/formats-table";
import { JobProgress } from "@/components/conversion/job-progress";
import type { Job } from "@/components/conversion/types";
import { DatasetFormatBadge } from "@/components/dataset-format";
import type { CatalogEntry } from "@/types/dataset-format.types";
type Local = CatalogEntry;
type SyncChange = {
  time: number;
  kind: "added" | "removed" | "updated" | "rebuilding" | "failed" | "skipped";
  name: string;
  detail: string;
};
type SyncStatus = {
  enabled: boolean;
  running: boolean;
  interval_seconds: number;
  last_scan: number | null;
  last_error: string | null;
  pending: string[];
  changes: SyncChange[];
};
type Catalog = {
  local: Local[];
  workspace: string;
  conversion_available: boolean;
  stages: string[];
};
export default function Workbench() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [path, setPath] = useState("");
  const [source, setSource] = useState("");
  // "Convert in the Workbench" links from the viewer pass ?source=<path>.
  useEffect(() => {
    const wanted = new URLSearchParams(window.location.search).get("source");
    if (wanted) setSource(wanted);
  }, []);
  const [stage, setStage] = useState("summary");
  const [fps, setFps] = useState(10);
  const [sourceFps, setSourceFps] = useState(30);
  const [output, setOutput] = useState("");
  const [options, setOptions] = useState("{}");
  const [plan, setPlan] = useState<Job | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { t } = useLocale();
  const [sync, setSync] = useState<SyncStatus | null>(null);
  const refresh = useCallback(async () => {
    const [c, j, st] = await Promise.all([
      leviApi<Catalog>("catalog"),
      leviApi<Job[]>("jobs"),
      leviApi<SyncStatus>("sync").catch(() => null),
    ]);
    setCatalog(c);
    setJobs(j);
    setSync(st);
  }, []);
  // Poll every second while anything runs (live progress), else every 4 s.
  const active = jobs.some(
    (j) => j.status === "running" || j.status === "queued",
  );
  useEffect(() => {
    refresh().catch((e) => setError(String(e)));
  }, [refresh]);
  useEffect(() => {
    const timer = setInterval(
      () => refresh().catch(() => {}),
      active ? 1000 : 4000,
    );
    return () => clearInterval(timer);
  }, [refresh, active]);
  async function action(fn: () => Promise<unknown>) {
    setError("");
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="levi-workbench">
      <span className="levi-eyebrow">
        <T>LEVI / CONVERSION & CURATION</T>
      </span>
      <h1>
        <T>From raw capture to reviewed data.</T>
      </h1>
      <p>
        <T>
          Register local datasets, inspect conversion plans, and follow every
          task through its logs. New outputs preserve the original capture.
        </T>
      </p>
      {error && (
        <p className="levi-error" role="alert">
          <T>{error}</T>
        </p>
      )}
      <section className="levi-box">
        <h2>
          <T>Local datasets</T>
        </h2>
        <form
          className="levi-row"
          onSubmit={(e) => {
            e.preventDefault();
            void action(async () => {
              await leviApi("catalog", { path });
              setPath("");
            });
          }}
        >
          <input
            className="levi-input grow"
            aria-label={t("Dataset directory")}
            placeholder={t(
              "LeRobot dataset (meta/info.json) or raw capture directory",
            )}
            value={path}
            required
            onChange={(e) => setPath(e.target.value)}
          />
          <button className="levi-primary" disabled={busy}>
            <T>Register & browse</T>
          </button>
        </form>
        <p className="mt-3 text-xs">
          <T>Workspace</T>: <code>{catalog?.workspace || "…"}</code>
        </p>
        <div className="levi-sync-bar">
          <span
            className={`levi-status ${
              sync?.last_error ? "fail" : sync?.running ? "pass" : "warn"
            }`}
          >
            {t(
              !sync?.enabled
                ? "Auto-sync off"
                : sync.running
                  ? "Auto-sync on"
                  : "Auto-sync paused",
            )}
          </span>
          <span>
            <T>
              New, changed and removed datasets in the workspace are picked up
              automatically.
            </T>
          </span>
          {sync?.last_scan && (
            <span className="tabular">
              {t("Last checked")}{" "}
              {new Date(sync.last_scan * 1000).toLocaleTimeString()}
            </span>
          )}
          {sync && sync.pending.length > 0 && (
            <span>
              {sync.pending.length} {t("waiting for copying to finish")}
            </span>
          )}
          <button
            type="button"
            className="levi-secondary"
            disabled={busy}
            onClick={() => void action(() => leviApi("sync", {}))}
          >
            <T>Sync now</T>
          </button>
        </div>
        {sync?.last_error && (
          <p className="levi-error mt-2">{t(sync.last_error)}</p>
        )}
        {sync && sync.changes.length > 0 && (
          <details className="levi-sync-changes">
            <summary className="cursor-pointer">
              <T>Recent workspace changes</T> ({sync.changes.length})
            </summary>
            <ul>
              {sync.changes.slice(0, 15).map((c) => (
                <li key={`${c.time}-${c.name}-${c.kind}`}>
                  <span className="tabular">
                    {new Date(c.time * 1000).toLocaleTimeString()}
                  </span>{" "}
                  <strong>{t(`sync.${c.kind}`)}</strong> {c.name}
                  {c.detail ? ` — ${t(c.detail)}` : ""}
                </li>
              ))}
            </ul>
          </details>
        )}
        <table className="levi-table">
          <thead>
            <tr>
              <th>
                <T>Dataset</T>
              </th>
              <th>
                <T>Format & version</T>
              </th>
              <th>
                <T>Episodes</T>
              </th>
              <th />
            </tr>
          </thead>
          <tbody>
            {catalog?.local.map((d) => (
              <tr key={d.id}>
                <td>
                  {d.kind !== "raw" || d.view_status === "ready" ? (
                    <Link className="text-cyan-300" href={`/${d.id}`}>
                      <T>{d.name}</T> ↗
                    </Link>
                  ) : (
                    <span>{d.name}</span>
                  )}
                  {d.view_status === "failed" && d.view_error && (
                    <p className="text-xs levi-fix">{t(d.view_error)}</p>
                  )}
                  <p className="text-xs break-all">
                    <T>{d.path}</T>
                  </p>
                </td>
                <td className="levi-format-cell">
                  <DatasetFormatBadge format={d.format} />
                </td>
                <td>{d.format?.episodes ?? d.info?.total_episodes ?? "—"}</td>
                <td>
                  <div className="flex flex-col gap-2 items-stretch">
                    <button
                      className="levi-secondary whitespace-nowrap"
                      onClick={() => setSource(d.path)}
                    >
                      <T>Use as input</T>
                    </button>
                    <button
                      className="text-xs text-slate-400 hover:text-red-300 whitespace-nowrap"
                      title={t(
                        "Remove from the list. Files, annotations and review flags are kept.",
                      )}
                      onClick={() => {
                        if (
                          window.confirm(
                            t(
                              "Remove this dataset from the list? Its files, annotations and review flags stay on disk. If it is still in the workspace, auto-sync will add it back.",
                            ),
                          )
                        )
                          void action(() =>
                            fetch(
                              `/api/levi/catalog/${encodeURIComponent(d.name)}`,
                              { method: "DELETE" },
                            ),
                          );
                      }}
                    >
                      <T>Unregister</T>
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {catalog?.local.length === 0 && (
          <p className="mt-4">
            <T>No local datasets registered yet.</T>
          </p>
        )}
      </section>
      <section className="levi-box">
        <div className="levi-row justify-between">
          <h2>
            <T>LEVI / </T>
            <T>Conversion pipeline</T>
          </h2>
          <span className="levi-status">
            <T>
              {catalog?.conversion_available ? "Built in" : "Install ffmpeg"}
            </T>
          </span>
        </div>
        <p>
          <T>
            Inspect an input to see which requirements it meets and which
            exports it supports, then choose an export and run it. Sources are
            never modified.
          </T>
        </p>
        <FormatsTable />
        <ConversionWizard
          jobs={jobs}
          refresh={refresh}
          available={!!catalog?.conversion_available}
          source={source}
          onSourceChange={setSource}
        />
      </section>
      <section className="levi-box">
        <details>
          <summary className="cursor-pointer">
            <T>Single stages (advanced)</T>
          </summary>
          <p>
            <T>
              Run one stage at a time. Review diagnostics and logs before
              continuing. Timestamp and task fixes create a full copy first.
            </T>
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void action(async () =>
                setPlan(
                  await leviApi<Job>("jobs/plan", {
                    source,
                    stage,
                    fps,
                    source_fps: sourceFps,
                    options: JSON.parse(options),
                    ...(output.trim() ? { output: output.trim() } : {}),
                  }),
                ),
              );
            }}
          >
            <label className="block mt-5 text-xs">
              <T>Input directory</T>
              <input
                className="levi-input w-full mt-2"
                value={source}
                onChange={(e) => {
                  setSource(e.target.value);
                  setPlan(null);
                }}
                required
                placeholder={t(
                  "Raw capture or previous stage output directory",
                )}
              />
            </label>
            <label className="block mt-4 text-xs">
              <T>Output directory (optional)</T>
              <input
                className="levi-input w-full mt-2"
                value={output}
                onChange={(e) => {
                  setOutput(e.target.value);
                  setPlan(null);
                }}
                placeholder={t(
                  "Leave blank to auto-name by job ID under LEVI_WORKSPACE",
                )}
              />
            </label>
            <div className="levi-row mt-4">
              <select
                aria-label={t("Conversion stage")}
                className="levi-input grow"
                value={stage}
                onChange={(e) => {
                  setStage(e.target.value);
                  setPlan(null);
                }}
              >
                {(catalog?.stages || ["stage-preview"])
                  .filter((s) => s !== "pipeline" && s !== "inspect")
                  .map((s) => (
                    <option key={s} value={s}>
                      {t(s)}
                    </option>
                  ))}
              </select>
              {["images", "pipeline", "fps"].includes(stage) && (
                <label className="text-xs">
                  <T>Source FPS</T>{" "}
                  <input
                    className="levi-input w-20"
                    type="number"
                    min="1"
                    max="240"
                    value={sourceFps}
                    onChange={(e) => {
                      setSourceFps(Number(e.target.value));
                      setPlan(null);
                    }}
                  />
                </label>
              )}
              <label className="text-xs">
                <T>FPS </T>
                <input
                  className="levi-input w-20"
                  type="number"
                  min="1"
                  max="240"
                  value={fps}
                  onChange={(e) => {
                    setFps(Number(e.target.value));
                    setPlan(null);
                  }}
                />
              </label>
              <button
                disabled={busy || !catalog?.conversion_available}
                className="levi-primary"
              >
                <T>Preview command</T>
              </button>
            </div>
            <details className="mt-5">
              <summary className="cursor-pointer">
                <T>Advanced conversion options</T>
              </summary>
              <p className="my-3 text-xs">
                <T>
                  JSON options: camera mapping, task mapping, excluded demo
                  paths, orientation, action mode and quality thresholds.
                </T>{" "}
                <Link href="/guide">
                  <T>Guide</T> ↗
                </Link>
              </p>
              <textarea
                aria-label={t("Conversion options JSON")}
                className="levi-input w-full font-mono text-xs"
                rows={8}
                value={options}
                onChange={(e) => {
                  setOptions(e.target.value);
                  setPlan(null);
                }}
              />
            </details>
          </form>
          {plan && (
            <div className="mt-6">
              <h3>
                <T>Review this plan</T>
              </h3>
              <pre>{plan.argv.map((arg) => JSON.stringify(arg)).join(" ")}</pre>
              <p className="my-3">
                <T>New output</T>: <T>{plan.output}</T>
              </p>
              <button
                className="levi-primary"
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    await leviApi(`jobs/${plan.id}/run`, {});
                    setPlan(null);
                  })
                }
              >
                <T>Run this plan</T> ↗
              </button>
            </div>
          )}
        </details>
      </section>
      <section className="levi-box">
        <h2>
          <T>Task history & logs</T>
        </h2>
        {jobs
          .filter((j) => j.status !== "planned")
          .map((j) => (
            <details key={j.id} className="border-b border-white/10 py-4">
              <summary className="cursor-pointer">
                <span className="levi-status mr-3">
                  <T>{j.status}</T>
                </span>
                <T>{j.stage}</T>
                <code className="ml-3 text-xs">
                  <T>{j.id}</T>
                </code>
              </summary>
              <JobProgress job={j} />
              {j.error && (
                <p className="levi-error" role="alert">
                  {t(j.error)}
                </p>
              )}
              <details>
                <summary className="cursor-pointer text-xs">
                  <T>Log</T>
                </summary>
                <pre>{j.log || t("Waiting for logs…")}</pre>
              </details>
              {j.exit_code !== undefined && (
                <p>
                  <T>Exit code</T>: {j.exit_code}
                </p>
              )}
              {j.result && (
                <details>
                  <summary>
                    <T>Structured report</T>
                  </summary>
                  <pre>{JSON.stringify(j.result, null, 2)}</pre>
                </details>
              )}
              <div className="levi-row mt-4">
                <button
                  className="levi-secondary"
                  disabled={j.status !== "succeeded" || !j.output_exists}
                  onClick={() => {
                    setSource(j.output);
                    setPlan(null);
                  }}
                >
                  <T>Use output as next input</T>
                </button>
                {j.dataset && (
                  <Link className="levi-primary" href={`/${j.dataset}`}>
                    <T>Review converted dataset</T> ↗
                  </Link>
                )}
              </div>
            </details>
          ))}
      </section>
      <p>
        <T>
          Review flags are saved per dataset. Export a review manifest from the
          viewer to hand off excluded episode IDs and notes.
        </T>
      </p>
    </main>
  );
}
