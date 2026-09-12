"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { DatasetTaskIndex } from "@/app/[org]/[dataset]/[episode]/fetch-data";
import HfAuthButton from "@/components/hf-auth-button";
import { T, useLocale } from "@/components/levi-locale";
import { useAuth } from "@/context/auth-context";
import { useTime } from "@/context/time-context";
import {
  cancelSam3Job,
  deleteSam3PromptPreset,
  editObjectAnnotation,
  fetchObjectAnnotations,
  fetchSam3Job,
  fetchSam3PromptPresets,
  getSam3Status,
  runSam3,
  saveSam3PromptPreset,
  type DatasetIdent,
} from "@/utils/annotationsClient";
import type {
  ObjectAnnotation,
  Sam3Capabilities,
  Sam3ItemError,
  Sam3JobProgress,
  Sam3PromptPreset,
} from "@/types/object-annotation.types";

type Props = {
  episodeId: number;
  ident: DatasetIdent;
  cameraKeys: string[];
  /** Every episode index in the dataset — powers the "all"/"range" scopes. */
  allEpisodes?: number[];
  /** Powers the "episodes matching the current task" scope. */
  taskIndex?: DatasetTaskIndex | null;
};

/** Which episodes a SAM3 run should cover, mirroring the scope pattern
 * already used by Action Insights (`CrossEpisodeScope` in fetch-data.ts). */
type AnnotationScope =
  | { kind: "range"; from: number; to: number }
  | { kind: "task"; task: string }
  | { kind: "all" };

function resolveScopeEpisodes(
  scope: AnnotationScope,
  allEpisodes: number[],
  taskIndex: DatasetTaskIndex | null | undefined,
): number[] {
  if (scope.kind === "all") return allEpisodes;
  if (scope.kind === "task") {
    if (!taskIndex) return [];
    return allEpisodes.filter((episode) =>
      (taskIndex.episodeTasks[episode] ?? []).includes(scope.task),
    );
  }
  const lo = Math.min(scope.from, scope.to);
  const hi = Math.max(scope.from, scope.to);
  if (!allEpisodes.length) {
    const range: number[] = [];
    for (let episode = lo; episode <= hi; episode += 1) range.push(episode);
    return range;
  }
  return allEpisodes.filter((episode) => episode >= lo && episode <= hi);
}

const statusColor: Record<ObjectAnnotation["status"], string> = {
  suggested: "text-cyan-300",
  accepted: "text-emerald-300",
  rejected: "text-red-300",
  needs_review: "text-amber-300",
};

function statusLabel(status: ObjectAnnotation["status"]): string {
  return {
    suggested: "suggested",
    accepted: "accepted",
    rejected: "rejected",
    needs_review: "needs review",
  }[status];
}

function formatBytes(value: number | undefined): string {
  if (!value || value < 1) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(value) / Math.log(1024)),
  );
  return (value / 1024 ** index).toFixed(index ? 1 : 0) + " " + units[index];
}

export default function ObjectAnnotationPanel({
  episodeId,
  ident,
  cameraKeys,
  allEpisodes,
  taskIndex,
}: Props) {
  const { language } = useLocale();
  const { oauth } = useAuth();
  const { seek } = useTime();
  const stableIdent = useMemo(
    () => ({
      repoId: ident.repoId ?? null,
      localPath: ident.localPath ?? null,
      revision: ident.revision ?? null,
    }),
    [ident.localPath, ident.repoId, ident.revision],
  );
  const [cameraKey, setCameraKey] = useState(cameraKeys[0] || "");
  const [promptText, setPromptText] = useState("object");
  const [objects, setObjects] = useState<ObjectAnnotation[]>([]);
  const [revision, setRevision] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<Sam3Capabilities | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [scope, setScope] = useState<AnnotationScope>({
    kind: "range",
    from: episodeId,
    to: episodeId,
  });
  const [runCameras, setRunCameras] = useState<Set<string>>(
    () => new Set(cameraKeys[0] ? [cameraKeys[0]] : []),
  );
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobProgress, setJobProgress] = useState<Sam3JobProgress | null>(null);
  const [itemErrors, setItemErrors] = useState<Sam3ItemError[]>([]);
  const [presets, setPresets] = useState<Sam3PromptPreset[]>([]);
  const [presetNameDraft, setPresetNameDraft] = useState("");
  const [selectedPresetName, setSelectedPresetName] = useState("");

  useEffect(() => {
    setCameraKey((current) =>
      cameraKeys.includes(current) ? current : cameraKeys[0] || "",
    );
    setRunCameras((current) => {
      const valid = [...current].filter((key) => cameraKeys.includes(key));
      if (valid.length) return new Set(valid);
      return new Set(cameraKeys[0] ? [cameraKeys[0]] : []);
    });
  }, [cameraKeys]);

  useEffect(() => {
    setScope({ kind: "range", from: episodeId, to: episodeId });
  }, [episodeId]);

  useEffect(() => {
    setObjects([]);
    setRevision(null);
    setMessage(null);
    setErrorDetail(null);
    setItemErrors([]);
  }, [stableIdent]);

  useEffect(() => {
    fetchSam3PromptPresets()
      .then(setPresets)
      .catch(() => setPresets([]));
  }, []);

  const refresh = useCallback(async () => {
    if (!cameraKey) return;
    try {
      const result = await fetchObjectAnnotations(episodeId, stableIdent, {
        cameraKey,
      });
      setObjects(result.objects);
      setRevision(result.revision);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  }, [cameraKey, episodeId, stableIdent]);

  const refreshStatus = useCallback(async () => {
    try {
      const result = await getSam3Status();
      setCapabilities(result);
      return result;
    } catch (error) {
      setCapabilities(null);
      setMessage(error instanceof Error ? error.message : String(error));
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    void refreshStatus();
    const timer = window.setInterval(() => {
      void refreshStatus();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [refreshStatus]);

  const tracks = useMemo(() => {
    const values = new Map<string, ObjectAnnotation>();
    for (const object of objects) {
      const key = object.object_id + ":" + object.track_id;
      if (!values.has(key)) values.set(key, object);
    }
    return [...values.values()].sort((a, b) => a.track_id - b.track_id);
  }, [objects]);

  const scopeEpisodes = useMemo(
    () => resolveScopeEpisodes(scope, allEpisodes ?? [], taskIndex),
    [scope, allEpisodes, taskIndex],
  );

  const runSam3Annotation = async () => {
    const prompts = promptText
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const cameras = [...runCameras];
    if (!prompts.length || !cameras.length || !scopeEpisodes.length) return;
    setBusy(true);
    setMessage(null);
    setErrorDetail(null);
    setItemErrors([]);
    setJobProgress(null);
    try {
      const result = await runSam3(stableIdent, {
        episode_indices: scopeEpisodes,
        camera_keys: cameras,
        prompts,
        start_frame: 0,
        max_frames: null,
        review_threshold: 0.6,
        accept_threshold: 0.9,
        provider: "sam3",
      });
      if (!result.job_id) throw new Error("SAM3 did not return a job ID");
      setJobId(result.job_id);
      setMessage(
        language === "zh"
          ? "SAM3 作业已启动，正在准备模型和标注…"
          : "SAM3 job started; preparing the model and annotations…",
      );
      // A batch over many episodes/cameras needs more than the ~3 minutes a
      // single-item run gets — scale the timeout with the batch size.
      const maxAttempts = Math.max(
        180,
        scopeEpisodes.length * cameras.length * 12,
      );
      for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const job = await fetchSam3Job(result.job_id, stableIdent);
        await refreshStatus();
        setJobProgress(job.progress ?? null);
        if (job.status === "succeeded") {
          setItemErrors(job.item_errors ?? []);
          setMessage(
            job.item_errors?.length
              ? language === "zh"
                ? `SAM3 建议已保存（${job.item_errors.length} 个片段/相机组合失败，见下方详情）`
                : `SAM3 suggestions saved (${job.item_errors.length} episode/camera pair(s) failed — see details below)`
              : language === "zh"
                ? "SAM3 建议已保存，请开始审核"
                : "SAM3 suggestions saved; review them now",
          );
          break;
        }
        if (job.status === "failed" || job.status === "cancelled") {
          if (job.error_detail) setErrorDetail(job.error_detail);
          setItemErrors(job.item_errors ?? []);
          throw new Error(String(job.error || "SAM3 job " + job.status));
        }
        if (attempt === maxAttempts - 1) throw new Error("SAM3 job timed out");
      }
      await refresh();
      window.dispatchEvent(new Event("levi:sam3-updated"));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
      setJobId(null);
      void refreshStatus();
    }
  };

  const cancelRun = async () => {
    if (!jobId) return;
    try {
      await cancelSam3Job(jobId, stableIdent);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const applyPreset = (name: string) => {
    const preset = presets.find((item) => item.name === name);
    if (preset) setPromptText(preset.prompts.join(", "));
  };

  const saveCurrentAsPreset = async () => {
    const name = presetNameDraft.trim();
    const prompts = promptText
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    if (!name || !prompts.length) return;
    try {
      const updated = await saveSam3PromptPreset({ name, prompts });
      setPresets(updated);
      setPresetNameDraft("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const deletePreset = async (name: string) => {
    try {
      setPresets(await deleteSam3PromptPreset(name));
      setSelectedPresetName("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const toggleRunCamera = (key: string) => {
    setRunCameras((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const edit = async (
    object: ObjectAnnotation,
    operation: "accept" | "reject",
  ) => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await editObjectAnnotation(stableIdent, {
        episode_index: episodeId,
        camera_key: cameraKey,
        operation,
        object_id: object.object_id,
        base_revision: revision,
      });
      setRevision(result.revision_id);
      await refresh();
      window.dispatchEvent(new Event("levi:sam3-updated"));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const download = capabilities?.download;
  const downloadPercent =
    typeof download?.percent === "number"
      ? Math.max(0, Math.min(100, download.percent))
      : null;
  const account = capabilities?.hf_auth;
  const workerReady =
    !!capabilities?.enabled &&
    !!capabilities.worker_project_present &&
    !!capabilities.worker_python_present;

  return (
    <section className="object-annotation-panel panel-raised">
      <div className="object-annotation-head">
        <div>
          <p className="section-kicker">
            <T>SAM3 object annotations</T>
          </p>
          <h2>
            <T>Objects, tracks and review</T>
          </h2>
        </div>
        <span className="object-annotation-badge">
          <T>sidecar · lossless RLE</T>
        </span>
      </div>
      <p className="object-annotation-copy">
        <T>
          Model suggestions stay separate from native LeRobot data. Review a
          track here to create a new annotation revision.
        </T>
      </p>

      <div className="object-annotation-status">
        <div className="object-annotation-status-head">
          <strong>
            <T>SAM3 runtime</T>
          </strong>
          <span className={workerReady ? "ready" : "muted"}>
            {workerReady ? <T>Ready</T> : <T>Setup required</T>}
          </span>
        </div>
        <div className="object-annotation-status-grid">
          <span>
            <T>Model</T>
          </span>
          <code>
            {capabilities?.model_repo || "1038lab/sam3"} /{" "}
            {capabilities?.model_filename || "sam3.pt"}
          </code>
          <span>
            <T>Hugging Face account</T>
          </span>
          <span className={account?.authenticated ? "ready" : "muted"}>
            {account?.authenticated
              ? account.username ||
                (oauth ? "signed in" : "environment account")
              : "not signed in"}
          </span>
          {!account?.authenticated && <HfAuthButton variant="ghost" />}
          <span>
            <T>Checkpoint location</T>
          </span>
          <code className="object-annotation-path">
            {capabilities?.checkpoint_path ||
              "workspace/checkpoints/sam3/sam3.pt"}
          </code>
        </div>
        {download && download.phase === "downloading" && (
          <div className="object-annotation-progress">
            <div className="object-annotation-progress-label">
              <span>
                <T>Downloading checkpoint</T>
              </span>
              <span>
                {downloadPercent === null
                  ? "…"
                  : downloadPercent.toFixed(1) + "%"}
                {" · "}
                {formatBytes(download.bytes)}
                {download.total_bytes
                  ? " / " + formatBytes(download.total_bytes)
                  : ""}
              </span>
            </div>
            <progress
              max={100}
              value={downloadPercent === null ? undefined : downloadPercent}
              aria-label="SAM3 checkpoint download progress"
            />
          </div>
        )}
        {download?.phase === "ready" && (
          <p className="object-annotation-runtime">
            <span className="ready">
              <T>Checkpoint ready</T>
            </span>
            <span>{formatBytes(download.bytes)}</span>
          </p>
        )}
        {download?.phase === "error" && (
          <p className="object-annotation-runtime muted">
            {download.message || "Checkpoint download failed"}
          </p>
        )}
        {!account?.authenticated && (
          <p className="object-annotation-runtime muted">
            <T>
              Sign in to Hugging Face to download the gated checkpoint. A local
              hf CLI login or HF_TOKEN is also accepted.
            </T>
          </p>
        )}
      </div>

      <div className="object-annotation-controls">
        <label>
          <span>
            <T>Camera</T>
          </span>
          <select
            value={cameraKey}
            onChange={(event) => setCameraKey(event.target.value)}
            disabled={!cameraKeys.length || busy}
          >
            {cameraKeys.map((key) => (
              <option key={key}>{key}</option>
            ))}
          </select>
        </label>
        <label className="object-annotation-prompt">
          <span>
            <T>Text prompts (comma separated)</T>
          </span>
          <input
            value={promptText}
            onChange={(event) => setPromptText(event.target.value)}
            placeholder="cup, plate, gripper"
            disabled={busy}
          />
        </label>
        <div className="object-annotation-presets">
          <select
            value={selectedPresetName}
            onChange={(event) => {
              setSelectedPresetName(event.target.value);
              if (event.target.value) applyPreset(event.target.value);
            }}
            disabled={busy || !presets.length}
          >
            <option value="">
              {language === "zh" ? "加载 prompt 预设…" : "Load prompt preset…"}
            </option>
            {presets.map((preset) => (
              <option key={preset.name} value={preset.name}>
                {preset.name}
              </option>
            ))}
          </select>
          {selectedPresetName && (
            <button
              type="button"
              onClick={() => void deletePreset(selectedPresetName)}
              disabled={busy}
              title="Delete this preset"
            >
              ×
            </button>
          )}
          <input
            value={presetNameDraft}
            onChange={(event) => setPresetNameDraft(event.target.value)}
            placeholder={language === "zh" ? "预设名称" : "preset name"}
            disabled={busy}
          />
          <button
            type="button"
            onClick={() => void saveCurrentAsPreset()}
            disabled={busy || !presetNameDraft.trim() || !promptText.trim()}
          >
            <T>Save preset</T>
          </button>
        </div>
        {busy && jobId ? (
          <button
            type="button"
            className="object-annotation-run"
            onClick={() => void cancelRun()}
          >
            <T>Cancel</T>
          </button>
        ) : (
          <button
            type="button"
            className="object-annotation-run sam3"
            onClick={() => void runSam3Annotation()}
            disabled={
              busy ||
              !runCameras.size ||
              !scopeEpisodes.length ||
              !promptText.trim() ||
              !workerReady
            }
            title="Uses the configured CUDA worker and 1038lab/sam3 checkpoint"
          >
            <T>Run SAM3 annotation</T>
          </button>
        )}
      </div>

      <div className="object-annotation-batch">
        <div className="object-annotation-batch-row">
          <label>
            <span>
              <T>Annotation scope</T>
            </span>
            <select
              value={scope.kind}
              onChange={(event) => {
                const kind = event.target.value as AnnotationScope["kind"];
                if (kind === "all") setScope({ kind: "all" });
                else if (kind === "task")
                  setScope({ kind: "task", task: taskIndex?.tasks[0] ?? "" });
                else
                  setScope({ kind: "range", from: episodeId, to: episodeId });
              }}
              disabled={busy}
            >
              <option value="range">
                {language === "zh" ? "片段范围" : "Episode range"}
              </option>
              {!!taskIndex?.tasks.length && (
                <option value="task">
                  {language === "zh" ? "按当前任务" : "By task"}
                </option>
              )}
              <option value="all">
                {language === "zh" ? "全部片段" : "All episodes"}
              </option>
            </select>
          </label>
          {scope.kind === "range" && (
            <div className="object-annotation-range">
              <input
                type="number"
                min={0}
                value={scope.from}
                onChange={(event) =>
                  setScope({
                    kind: "range",
                    from: Number(event.target.value),
                    to: scope.to,
                  })
                }
                disabled={busy}
              />
              <span>–</span>
              <input
                type="number"
                min={0}
                value={scope.to}
                onChange={(event) =>
                  setScope({
                    kind: "range",
                    from: scope.from,
                    to: Number(event.target.value),
                  })
                }
                disabled={busy}
              />
            </div>
          )}
          {scope.kind === "task" && (
            <select
              value={scope.task}
              onChange={(event) =>
                setScope({ kind: "task", task: event.target.value })
              }
              disabled={busy}
            >
              {(taskIndex?.tasks ?? []).map((task) => (
                <option key={task} value={task}>
                  {task}
                </option>
              ))}
            </select>
          )}
          <span className="object-annotation-scope-count">
            {scopeEpisodes.length} <T>episode(s)</T>
          </span>
        </div>
        {cameraKeys.length > 1 && (
          <div className="object-annotation-camera-checks">
            <span>
              <T>Run on cameras</T>
            </span>
            {cameraKeys.map((key) => (
              <label key={key} className="object-annotation-checkbox">
                <input
                  type="checkbox"
                  checked={runCameras.has(key)}
                  onChange={() => toggleRunCamera(key)}
                  disabled={busy}
                />
                {key}
              </label>
            ))}
          </div>
        )}
      </div>

      {busy && jobProgress && jobProgress.total > 1 && (
        <div className="object-annotation-progress">
          <div className="object-annotation-progress-label">
            <span>
              <T>Annotating batch</T>
            </span>
            <span>
              {jobProgress.done}/{jobProgress.total}
              {jobProgress.current_episode != null &&
                ` · episode ${jobProgress.current_episode}`}
              {jobProgress.current_camera && ` · ${jobProgress.current_camera}`}
            </span>
          </div>
          <progress
            max={jobProgress.total}
            value={jobProgress.done}
            aria-label="SAM3 batch annotation progress"
          />
        </div>
      )}

      <div className="object-annotation-runtime">
        <span className={capabilities?.enabled ? "ready" : "muted"}>
          {capabilities?.enabled ? (
            <T>SAM3 is globally enabled</T>
          ) : (
            <T>SAM3 is disabled</T>
          )}
        </span>
        {capabilities?.model_revision && (
          <span>
            <T>revision</T> {capabilities.model_revision}
          </span>
        )}
        {revision && <code>{revision.slice(0, 16)}</code>}
      </div>

      {message && <p className="object-annotation-message">{message}</p>}
      {errorDetail && (
        <details className="object-annotation-error-detail">
          <summary>
            <T>Show worker log</T>
          </summary>
          <pre>{errorDetail}</pre>
        </details>
      )}
      {itemErrors.length > 0 && (
        <details className="object-annotation-error-detail">
          <summary>
            {itemErrors.length}{" "}
            <T>episode/camera pair(s) failed in this batch</T>
          </summary>
          <pre>
            {itemErrors
              .map(
                (item) =>
                  `episode ${item.episode_index} · ${item.camera_key}: ${item.error}`,
              )
              .join("\n")}
          </pre>
        </details>
      )}
      {!tracks.length ? (
        <p className="object-annotation-empty">
          <T>No object suggestions for this camera yet.</T>
        </p>
      ) : (
        <div className="object-annotation-list">
          {tracks.map((object) => (
            <article
              className="object-annotation-row"
              key={object.object_id + ":" + object.track_id}
            >
              <button
                type="button"
                className="object-annotation-main"
                onClick={() => seek(object.timestamp)}
                title="Jump to first frame"
              >
                <span className="object-track-id">#{object.track_id}</span>
                <span className="object-concept">{object.concept}</span>
                <span className={statusColor[object.status]}>
                  <T>{statusLabel(object.status)}</T>
                </span>
                <span className="object-score">
                  {(object.score * 100).toFixed(0)}%
                </span>
                <span className="object-frame">f{object.frame_index}</span>
              </button>
              <div className="object-annotation-actions">
                <button
                  type="button"
                  onClick={() => void edit(object, "accept")}
                  disabled={busy}
                >
                  <T>Accept</T>
                </button>
                <button
                  type="button"
                  onClick={() => void edit(object, "reject")}
                  disabled={busy}
                >
                  <T>Reject</T>
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
