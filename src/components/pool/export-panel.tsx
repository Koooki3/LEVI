"use client";
import { useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { leviRequest } from "@/components/levi-api";
import { PoolJobProgress, RUNNING, StatusBadge } from "./pool-progress";
import {
  FORMAT_LABELS,
  REASON_LABELS,
  TIMING_HINTS,
  TIMING_LABELS,
  defaultTiming,
  stopsExport,
  type ExportFormat,
  type PoolJob,
  type Preview,
  type Recipe,
  type Timing,
} from "./types";

const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const DEFAULT_CAMERAS =
  "wrist_camera=observation.images.hand\nside_camera=observation.images.view1";

function pairs(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const [key, ...rest] = line.split("=");
    if (key.trim() && rest.join("=").trim())
      out[key.trim()] = rest.join("=").trim();
  }
  return out;
}

/** Where the export may go: a relative name lands in the workspace's export
 * folder; an absolute folder must lie inside LEVI_EXPORT_ROOTS. */
export function outputDirProblem(dir: string, roots: string[]): string | null {
  const value = dir.trim();
  if (!value) return null;
  if (!value.startsWith("/") && !value.startsWith("~"))
    return "Give an absolute folder, or leave it empty for the workspace's export folder.";
  if (value.startsWith("~")) return null;
  const clean = value.replace(/\/+$/, "");
  const inside = roots.some((root) => {
    const base = root.replace(/\/+$/, "");
    return clean === base || clean.startsWith(base + "/");
  });
  return inside ? null : "outside";
}

export function ExportPanel({
  recipe,
  preview,
  exportRoots,
  job,
  humanAsSuccess,
  onHumanAsSuccess,
  format,
  onFormat,
  fps,
  onFps,
  timing,
  onTiming,
  onJob,
  onPush,
}: {
  recipe: Recipe;
  preview: Preview | null;
  exportRoots: string[];
  job: PoolJob | null;
  humanAsSuccess: boolean;
  onHumanAsSuccess: (value: boolean) => void;
  format: ExportFormat;
  onFormat: (format: ExportFormat) => void;
  fps: number;
  onFps: (value: number) => void;
  /** The chosen timing; null = the format's own default. */
  timing: Timing | null;
  onTiming: (value: Timing) => void;
  onJob: (job: PoolJob) => void;
  onPush: (job: PoolJob) => void;
}) {
  const { t } = useLocale();
  const [name, setName] = useState("");
  const [outputDir, setOutputDir] = useState("");
  const [cameras, setCameras] = useState(DEFAULT_CAMERAS);
  const [cameraMap, setCameraMap] = useState("");
  const [hardlink, setHardlink] = useState(false);
  const [plan, setPlan] = useState<PoolJob | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dirProblem = outputDirProblem(outputDir, exportRoots);
  const nameProblem = name && !NAME.test(name);
  const effectiveTiming = timing ?? defaultTiming(format);
  const stopped = !!preview?.warnings?.some(stopsExport);
  const canRun =
    recipe.tasks.length > 0 &&
    !stopped &&
    !!name &&
    !nameProblem &&
    !dirProblem &&
    !busy &&
    !(job && RUNNING.has(job.status));
  async function start(dryRun: boolean) {
    setError("");
    setBusy(true);
    try {
      const result = await leviRequest<PoolJob>("POST", "pool/export", {
        recipe: { ...recipe, name: recipe.name || "untitled" },
        options: {
          format,
          name,
          output_dir: outputDir.trim() || null,
          fps,
          ...(effectiveTiming ? { timing: effectiveTiming } : {}),
          cameras: pairs(cameras),
          camera_map: pairs(cameraMap),
          hardlink: format === "raw_capture" && hardlink,
          human_as_success: format === "recap_value" && humanAsSuccess,
        },
        dry_run: dryRun,
      });
      if (dryRun) setPlan(result);
      else {
        setPlan(null);
        onJob(result);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const done = job && job.status === "succeeded" && job.kind === "export";
  return (
    <section className="levi-pool-card" aria-labelledby="pool-export">
      <h2 id="pool-export">{t("Export")}</h2>
      <div className="levi-pool-fields">
        <label className="wide">
          <span>{t("Format")}</span>
          <select
            className="levi-input"
            value={format}
            onChange={(e) => {
              onFormat(e.target.value as ExportFormat);
            }}
          >
            {(Object.keys(FORMAT_LABELS) as ExportFormat[]).map((f) => (
              <option key={f} value={f}>
                {t(FORMAT_LABELS[f])}
              </option>
            ))}
          </select>
        </label>
        <label className="wide">
          <span>{t("Dataset name")}</span>
          <input
            className="levi-input"
            value={name}
            required
            aria-invalid={!!nameProblem}
            placeholder="pi05-mix-v1"
            onChange={(e) => setName(e.target.value)}
          />
          {nameProblem && (
            <small className="levi-pool-bad">
              {t("Letters, digits, dot, dash and underscore")}
            </small>
          )}
        </label>
        <label className="wide">
          <span>{t("Output directory")}</span>
          <input
            className="levi-input"
            value={outputDir}
            aria-invalid={!!dirProblem}
            aria-describedby="pool-export-roots"
            placeholder={t("Empty: the workspace's export folder")}
            onChange={(e) => setOutputDir(e.target.value)}
          />
          {dirProblem && (
            <small className="levi-pool-bad" role="alert">
              {dirProblem === "outside"
                ? `${t("This folder is outside LEVI_EXPORT_ROOTS.")} ${t("Allowed")}: ${exportRoots.join(", ")}`
                : t(dirProblem)}
            </small>
          )}
          <small id="pool-export-roots" className="levi-pool-hint">
            LEVI_EXPORT_ROOTS: {exportRoots.join(", ") || "—"}
          </small>
        </label>
        {format !== "raw_capture" && (
          <>
            <label>
              <span>{t("FPS")}</span>
              <input
                className="levi-input"
                type="number"
                min={1}
                max={240}
                value={fps}
                onChange={(e) => onFps(Number(e.target.value) || 10)}
              />
            </label>
            <label className="wide">
              <span>{t("Timing")}</span>
              <select
                className="levi-input"
                value={effectiveTiming ?? "resample"}
                aria-describedby="pool-export-timing-hint"
                onChange={(e) => onTiming(e.target.value as Timing)}
              >
                {(Object.keys(TIMING_LABELS) as Timing[]).map((m) => (
                  <option key={m} value={m}>
                    {t(TIMING_LABELS[m])}
                  </option>
                ))}
              </select>
              <small id="pool-export-timing-hint" className="levi-pool-hint">
                {t(TIMING_HINTS[effectiveTiming ?? "resample"])}
              </small>
            </label>
            <label className="wide">
              <span>{t("Raw capture cameras (camera=output key)")}</span>
              <textarea
                className="levi-input"
                rows={2}
                value={cameras}
                onChange={(e) => setCameras(e.target.value)}
              />
            </label>
            <label className="wide">
              <span>{t("LeRobot camera keys (source key=output key)")}</span>
              <textarea
                className="levi-input"
                rows={2}
                placeholder="observation.images.wrist=observation.images.hand"
                value={cameraMap}
                onChange={(e) => setCameraMap(e.target.value)}
              />
            </label>
          </>
        )}
        {format === "recap_value" && (
          <label className="levi-pool-check wide">
            <input
              type="checkbox"
              checked={humanAsSuccess}
              onChange={() => onHumanAsSuccess(!humanAsSuccess)}
            />
            <span>
              {t("Count human demonstrations without an outcome as success")}
            </span>
          </label>
        )}
        {format === "raw_capture" && (
          <label className="levi-pool-check wide">
            <input
              type="checkbox"
              checked={hardlink}
              onChange={() => setHardlink(!hardlink)}
            />
            <span>
              {t(
                "Hard-link videos instead of copying (small files are always copied)",
              )}
            </span>
          </label>
        )}
      </div>
      <p className="levi-pool-hint">
        {preview
          ? `${preview.episodes.toLocaleString()} ${t("episodes")} · ${preview.frames.toLocaleString()} ${t("frames")} · ${preview.excluded_heldout.toLocaleString()} ${t("held-out excluded")}`
          : t("Choose tasks to see a preview.")}
      </p>
      {stopped && (
        <p className="levi-pool-bad" role="alert">
          {t("Resolve the blocking notes in the composition first.")}
        </p>
      )}
      <div className="levi-row">
        <button
          type="button"
          className="levi-secondary"
          disabled={!canRun}
          onClick={() => void start(true)}
        >
          {t("Dry run")}
        </button>
        <button
          type="button"
          className="levi-primary"
          disabled={!canRun}
          onClick={() => void start(false)}
        >
          {t("Start export")}
        </button>
      </div>
      {error && (
        <p className="levi-error" role="alert">
          {t(error)}
        </p>
      )}
      {plan && (
        <div className="levi-pool-plan">
          <p>
            <strong>{t("Dry run")}</strong>: {plan.planned_episodes}{" "}
            {t("episodes")} → <code>{plan.target}</code>
          </p>
          <p className="levi-pool-hint">
            {plan.planned_excluded} {t("left out")} · {t("nothing was written")}
          </p>
        </div>
      )}
      {job && job.kind === "export" && (
        <div className="levi-pool-job">
          <p className="levi-row">
            <StatusBadge status={job.status} />
            <code className="levi-pool-ellipsis">
              {job.result?.dataset_path || job.target}
            </code>
          </p>
          <PoolJobProgress job={job} />
          {job.error && <p className="levi-error">{t(job.error)}</p>}
          {done && (
            <>
              <p className="levi-pool-hint">
                {job.result?.episodes?.toLocaleString()} {t("episodes")} ·{" "}
                {job.result?.frames?.toLocaleString()} {t("frames")}
                {Object.entries(job.result?.excluded || {}).map(
                  ([reason, n]) =>
                    ` · ${t(REASON_LABELS[reason] || reason)} ${n}`,
                )}
              </p>
              <div className="levi-row">
                <a
                  className="levi-secondary"
                  href={`/api/levi/pool/jobs/${encodeURIComponent(job.id)}/summary`}
                  target="_blank"
                  rel="noreferrer"
                >
                  pool_export.json ↗
                </a>
                <button
                  type="button"
                  className="levi-primary"
                  onClick={() => onPush(job)}
                >
                  {t("Send to remote")}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}
