"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { T, useLocale } from "@/components/levi-locale";
import { leviApi } from "@/components/levi-api";
type Local = {
  id: string;
  name: string;
  path: string;
  info: {
    total_episodes: number;
    total_frames: number;
    codebase_version: string;
  };
};
type Catalog = {
  local: Local[];
  workspace: string;
  conversion_available: boolean;
  stages: string[];
};
type Job = {
  id: string;
  stage: string;
  source: string;
  output: string;
  argv: string[];
  status: string;
  log?: string;
  error?: string;
  dataset?: string;
  exit_code?: number;
  output_exists?: boolean;
  result?: Record<string, unknown>;
};
export default function Workbench() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [path, setPath] = useState("");
  const [source, setSource] = useState("");
  const [stage, setStage] = useState("pipeline");
  const [fps, setFps] = useState(10);
  const [sourceFps, setSourceFps] = useState(30);
  const [output, setOutput] = useState("");
  const [options, setOptions] = useState("{}");
  const [plan, setPlan] = useState<Job | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const { t } = useLocale();
  async function refresh() {
    const [c, j] = await Promise.all([
      leviApi<Catalog>("catalog"),
      leviApi<Job[]>("jobs"),
    ]);
    setCatalog(c);
    setJobs(j);
  }
  useEffect(() => {
    refresh().catch((e) => setError(String(e)));
    const timer = setInterval(() => refresh().catch(() => {}), 4000);
    return () => clearInterval(timer);
  }, []);
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
            placeholder={t("Dataset directory containing meta/info.json")}
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
        <table className="levi-table">
          <thead>
            <tr>
              <th>
                <T>Dataset</T>
              </th>
              <th>
                <T>Version</T>
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
                  <Link className="text-cyan-300" href={`/${d.id}`}>
                    <T>{d.name}</T> ↗
                  </Link>
                  <p className="text-xs break-all">
                    <T>{d.path}</T>
                  </p>
                </td>
                <td>
                  <T>{d.info.codebase_version}</T>
                </td>
                <td>
                  <T>{d.info.total_episodes}</T>
                </td>
                <td>
                  <button
                    className="levi-secondary"
                    onClick={() => setSource(d.path)}
                  >
                    <T>Use as input</T>
                  </button>
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
              placeholder={t("Raw capture or previous stage output directory")}
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
                "Leave blank to auto-name by job ID under LEVI_WORKSPACE/datasets",
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
              {(catalog?.stages || ["stage-preview"]).map((s) => (
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
                JSON options: camera mapping, task mapping, excluded demo paths,
                orientation, action mode and quality thresholds.
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
              <pre>{j.log || j.error || t("Waiting for logs…")}</pre>
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
