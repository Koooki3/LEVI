"use client";
import "@/components/pages-ui/pages.css";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { T, useLocale } from "@/components/levi-locale";
import { leviApi } from "@/components/levi-api";
import { useConfirmAction } from "@/components/shell/confirm";
import { ConversionWizard } from "@/components/conversion/conversion-wizard";
import { FormatsTable } from "@/components/conversion/formats-table";
import { JobProgress } from "@/components/conversion/job-progress";
import type { Job } from "@/components/conversion/types";
import { DatasetFormatBadge } from "@/components/dataset-format";
import {
  ArrowRight,
  ArrowUpRight,
  Database,
  FolderInput,
  History,
  Play,
  RefreshCw,
  Trash2,
} from "lucide-react";
import {
  Badge,
  Button,
  EmptyState,
  Icon,
  Skeleton,
  Tooltip,
} from "@/components/ds";
import { EmptyLine, RequestProblem } from "@/components/pages-ui/feedback";
import { useServerText } from "@/components/pages-ui/messages";
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
const JOB_TONE: Record<string, "success" | "warning" | "danger" | "info"> = {
  succeeded: "success",
  running: "info",
  queued: "info",
  failed: "danger",
  cancelled: "warning",
  interrupted: "warning",
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
  // What the failed action was trying to do (the error box's title).
  const [errorWhat, setErrorWhat] = useState("The request did not complete");
  // The last read of the page's state failed (null: the answer is unknown).
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const { t } = useLocale();
  const serverText = useServerText();
  const confirm = useConfirmAction();
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
    setLoadError("");
  }, []);
  const reload = useCallback(
    () =>
      refresh().catch((e) =>
        setLoadError(e instanceof Error ? e.message : String(e)),
      ),
    [refresh],
  );
  // Poll every second while anything runs (live progress), else every 4 s.
  const active = jobs.some(
    (j) => j.status === "running" || j.status === "queued",
  );
  useEffect(() => {
    void reload();
  }, [reload]);
  useEffect(() => {
    const timer = setInterval(() => void reload(), active ? 1000 : 4000);
    return () => clearInterval(timer);
  }, [reload, active]);
  const loading = catalog === null && !loadError;
  async function action(
    fn: () => Promise<unknown>,
    what = "The request did not complete",
  ) {
    setError("");
    setErrorWhat(what);
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError(
        e instanceof SyntaxError
          ? t("The conversion options are not valid JSON.")
          : e instanceof Error
            ? e.message
            : String(e),
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="ds-root pg-workbench">
      <h1>
        <T>From raw capture to reviewed data.</T>
      </h1>
      <p>
        <T>
          Register local datasets, inspect conversion plans, and follow every
          task through its logs. New outputs preserve the original capture.
        </T>
      </p>
      {loadError && (
        <RequestProblem
          action="The page could not read LEVI's state"
          message={loadError}
          live={false}
          onRetry={() => void reload()}
        />
      )}
      {error && <RequestProblem action={errorWhat} message={error} />}
      <section className="pg-box pg-pool-teaser">
        <div>
          <h2>
            <T>Training pool</T>
          </h2>
          <p>
            <T>
              Every dataset on this machine by task: compose tasks in order,
              preview, export for π0.5 or RECAP and send it to a training
              machine.
            </T>
          </p>
        </div>
        <Link className="ds-btn ds-btn--secondary ds-focus" href="/pool">
          <T>Open the training pool</T>
          <Icon icon={ArrowRight} />
        </Link>
      </section>
      <section className="pg-box">
        <h2>
          <T>Local datasets</T>
        </h2>
        <form
          className="pg-row"
          onSubmit={(e) => {
            e.preventDefault();
            void action(async () => {
              await leviApi("catalog", { path });
              setPath("");
            }, "The dataset was not registered");
          }}
        >
          <input
            className="ds-input ds-focus grow"
            aria-label={t("Dataset directory")}
            placeholder={t(
              "LeRobot dataset (meta/info.json) or raw capture directory",
            )}
            value={path}
            required
            onChange={(e) => setPath(e.target.value)}
          />
          <Button type="submit" icon={FolderInput} disabled={busy}>
            <T>Register & browse</T>
          </Button>
        </form>
        <p className="pg-mt-3 pg-small">
          <T>Workspace</T>: <code>{catalog?.workspace || "…"}</code>
        </p>
        <div className="pg-sync-bar">
          <Badge
            tone={
              !sync
                ? "neutral"
                : sync.last_error
                  ? "danger"
                  : sync.running
                    ? "success"
                    : "warning"
            }
          >
            {t(
              !sync
                ? "Auto-sync: cannot tell right now"
                : !sync.enabled
                  ? "Auto-sync off"
                  : sync.running
                    ? "Auto-sync on"
                    : "Auto-sync paused",
            )}
          </Badge>
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
          <Button
            size="sm"
            icon={RefreshCw}
            disabled={busy || !catalog}
            onClick={() =>
              void action(() => leviApi("sync", {}), "The sync did not run")
            }
          >
            <T>Sync now</T>
          </Button>
        </div>
        {sync?.last_error && (
          <RequestProblem
            action="Auto-sync stopped on an error"
            message={sync.last_error}
            fix={t("Press Sync now to try again.")}
          />
        )}
        {sync && sync.changes.length > 0 && (
          <details className="pg-sync-changes">
            <summary className="">
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
        {loading && (
          <div className="pg-gap-top" aria-busy="true">
            <span className="sr-only" role="status">
              {t("Loading…")}
            </span>
            <Skeleton height={96} radius="md" />
          </div>
        )}
        {catalog && catalog.local.length !== 0 && (
          <div className="ds-table-wrap pg-gap-top">
            <table className="ds-table">
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
                  <th>
                    <span className="sr-only">
                      <T>Actions</T>
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {catalog.local.map((d) => (
                  <tr key={d.id}>
                    <td>
                      {d.kind !== "raw" || d.view_status === "ready" ? (
                        <Link href={`/${d.id}`}>
                          <T>{d.name}</T>
                          <Icon icon={ArrowUpRight} />
                        </Link>
                      ) : (
                        <span>{d.name}</span>
                      )}
                      {d.view_status === "failed" && d.view_error && (
                        <p className="pg-small pg-fix">
                          {serverText(d.view_error)}
                        </p>
                      )}
                      <p className="pg-small pg-break">
                        <T>{d.path}</T>
                      </p>
                    </td>
                    <td className="pg-format-cell">
                      <DatasetFormatBadge format={d.format} />
                    </td>
                    <td className="ds-num">
                      {d.format?.episodes ?? d.info?.total_episodes ?? "—"}
                    </td>
                    <td>
                      <div className="pg-stack">
                        <Button size="sm" onClick={() => setSource(d.path)}>
                          <T>Use as input</T>
                        </Button>
                        <Tooltip
                          content={t(
                            "Remove from the list. Files, annotations and review flags are kept.",
                          )}
                        >
                          <Button
                            size="sm"
                            variant="ghost"
                            icon={Trash2}
                            onClick={async () => {
                              if (
                                await confirm({
                                  title: t(
                                    "Remove {name} from the list?",
                                  ).replace("{name}", d.name),
                                  description: t(
                                    "Remove this dataset from the list? Its files, annotations and review flags stay on disk. If it is still in the workspace, auto-sync will add it back.",
                                  ),
                                  confirmLabel: t("Unregister"),
                                })
                              )
                                void action(
                                  () =>
                                    fetch(
                                      `/api/levi/catalog/${encodeURIComponent(d.name)}`,
                                      { method: "DELETE" },
                                    ),
                                  "The dataset was not removed from the list",
                                );
                            }}
                          >
                            <T>Unregister</T>
                          </Button>
                        </Tooltip>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {catalog && catalog.local.length === 0 && (
          <EmptyState
            icon={Database}
            title={t("No local datasets registered yet.")}
            description={t(
              "Enter a LeRobot dataset or a raw capture folder above and press Register & browse.",
            )}
          />
        )}
      </section>
      <section className="pg-box">
        <div className="pg-row pg-between">
          <h2>
            <T>Conversion pipeline</T>
          </h2>
          <Badge
            tone={
              !catalog
                ? "neutral"
                : catalog.conversion_available
                  ? "success"
                  : "warning"
            }
          >
            <T>
              {!catalog
                ? "Cannot tell right now"
                : catalog.conversion_available
                  ? "Built in"
                  : "Install ffmpeg"}
            </T>
          </Badge>
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
          available={catalog ? !!catalog.conversion_available : null}
          source={source}
          onSourceChange={setSource}
        />
      </section>
      <section className="pg-box">
        <details>
          <summary className="">
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
              void action(
                async () =>
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
                "The command could not be previewed",
              );
            }}
          >
            <label className="pg-block pg-mt-5 pg-small">
              <T>Input directory</T>
              <input
                className="ds-input ds-focus pg-full pg-mt-2"
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
            <label className="pg-block pg-mt-4 pg-small">
              <T>Output directory (optional)</T>
              <input
                className="ds-input ds-focus pg-full pg-mt-2"
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
            <div className="pg-row pg-mt-4">
              <select
                aria-label={t("Conversion stage")}
                className="ds-input ds-focus grow"
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
                <label className="pg-small">
                  <T>Source FPS</T>{" "}
                  <input
                    className="ds-input ds-focus pg-w-num"
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
              <label className="pg-small">
                <T>FPS </T>
                <input
                  className="ds-input ds-focus pg-w-num"
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
              <Button
                type="submit"
                disabled={busy || !catalog?.conversion_available}
                aria-describedby={
                  catalog?.conversion_available ? undefined : "wb-plan-why"
                }
              >
                <T>Preview command</T>
              </Button>
            </div>
            {!catalog?.conversion_available && (
              <p id="wb-plan-why" className="pg-small pg-mt-2">
                {t(
                  catalog
                    ? "Previewing a command needs ffmpeg, which was not found."
                    : "Previewing is off until the page can read LEVI's state.",
                )}
              </p>
            )}
            <details className="pg-mt-5">
              <summary className="">
                <T>Advanced conversion options</T>
              </summary>
              <p className="pg-my-3 pg-small">
                <T>
                  JSON options: camera mapping, task mapping, excluded demo
                  paths, orientation, action mode and quality thresholds.
                </T>{" "}
                <Link href="/guide">
                  <T>Guide</T>
                  <Icon icon={ArrowUpRight} />
                </Link>
              </p>
              <textarea
                aria-label={t("Conversion options JSON")}
                className="ds-input ds-focus pg-full pg-mono pg-small"
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
            <div className="pg-mt-6">
              <h3>
                <T>Review this plan</T>
              </h3>
              <pre>{plan.argv.map((arg) => JSON.stringify(arg)).join(" ")}</pre>
              <p className="pg-my-3">
                <T>New output</T>: <T>{plan.output}</T>
              </p>
              <Button
                icon={Play}
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    await leviApi(`jobs/${plan.id}/run`, {});
                    setPlan(null);
                  }, "The plan did not start")
                }
              >
                <T>Run this plan</T>
              </Button>
            </div>
          )}
        </details>
      </section>
      <section className="pg-box">
        <h2>
          <T>Task history & logs</T>
        </h2>
        {jobs.every((j) => j.status === "planned") && (
          <EmptyLine icon={History}>
            {t("No conversion has run yet. Runs and their logs appear here.")}
          </EmptyLine>
        )}
        {jobs
          .filter((j) => j.status !== "planned")
          .map((j) => (
            <details key={j.id} className="pg-history-item">
              <summary>
                <Badge
                  tone={JOB_TONE[j.status] ?? "neutral"}
                  className="pg-mr-3"
                >
                  <T>{j.status}</T>
                </Badge>
                <T>{j.stage}</T>
                <code className="pg-ml-3 pg-small">
                  <T>{j.id}</T>
                </code>
              </summary>
              <JobProgress job={j} />
              {j.error && (
                <RequestProblem
                  action="The conversion did not finish"
                  message={j.error}
                  fix={t("Open the log below for the step that failed.")}
                />
              )}
              <details>
                <summary className="pg-small">
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
              <div className="pg-row pg-mt-4">
                <Button
                  disabled={j.status !== "succeeded" || !j.output_exists}
                  onClick={() => {
                    setSource(j.output);
                    setPlan(null);
                  }}
                >
                  <T>Use output as next input</T>
                </Button>
                {j.dataset && (
                  <Link
                    className="ds-btn ds-btn--secondary ds-focus"
                    href={`/${j.dataset}`}
                  >
                    <T>Review converted dataset</T>
                    <Icon icon={ArrowUpRight} />
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
