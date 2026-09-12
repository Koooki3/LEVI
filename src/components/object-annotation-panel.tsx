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
  planSam3,
  runSam3,
  saveSam3PromptPreset,
  startSam3CheckpointDownload,
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
  if (scope.kind === "all") return [...allEpisodes];
  if (scope.kind === "task") {
    if (!taskIndex) return [];
    return allEpisodes.filter((episode) =>
      (taskIndex.episodeTasks[episode] ?? []).includes(scope.task),
    );
  }
  const lo = Math.min(scope.from, scope.to);
  const hi = Math.max(scope.from, scope.to);
  return allEpisodes.filter((episode) => episode >= lo && episode <= hi);
}

function parsePrompts(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[,;\n]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

type PlanState = "idle" | "checking" | "ready";

type TrackSummary = {
  object: ObjectAnnotation;
  frameCount: number;
  startFrame: number;
  endFrame: number;
  meanScore: number;
  minScore: number;
};

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
  const [promptText, setPromptText] = useState("");
  const [objects, setObjects] = useState<ObjectAnnotation[]>([]);
  const [revision, setRevision] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<Sam3Capabilities | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
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
  const [planState, setPlanState] = useState<PlanState>("idle");
  const [planId, setPlanId] = useState<string | null>(null);
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
    setPlanState("idle");
    setPlanId(null);
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

  useEffect(() => {
    const onAuthChanged = () => {
      void refreshStatus();
    };
    window.addEventListener("levi:hf-auth-changed", onAuthChanged);
    return () =>
      window.removeEventListener("levi:hf-auth-changed", onAuthChanged);
  }, [refreshStatus]);

  useEffect(() => {
    if (capabilities?.download?.phase !== "downloading") return;
    const timer = window.setInterval(() => {
      void refreshStatus();
    }, 1000);
    return () => window.clearInterval(timer);
  }, [capabilities?.download?.phase, refreshStatus]);

  const episodeUniverse = useMemo(() => {
    const values = allEpisodes?.length ? allEpisodes : [episodeId];
    return [...new Set(values)].sort((a, b) => a - b);
  }, [allEpisodes, episodeId]);

  const scopeEpisodes = useMemo(
    () => resolveScopeEpisodes(scope, episodeUniverse, taskIndex),
    [scope, episodeUniverse, taskIndex],
  );

  const trackSummaries = useMemo<TrackSummary[]>(() => {
    const groups = new Map<string, TrackSummary>();
    for (const object of objects) {
      const key = object.object_id + ":" + object.track_id;
      const current = groups.get(key);
      if (!current) {
        groups.set(key, {
          object,
          frameCount: 1,
          startFrame: object.frame_index,
          endFrame: object.frame_index,
          meanScore: object.score,
          minScore: object.score,
        });
        continue;
      }
      current.frameCount += 1;
      if (object.frame_index < current.object.frame_index) {
        current.object = object;
      }
      current.startFrame = Math.min(current.startFrame, object.frame_index);
      current.endFrame = Math.max(current.endFrame, object.frame_index);
      current.meanScore =
        (current.meanScore * (current.frameCount - 1) + object.score) /
        current.frameCount;
      current.minScore = Math.min(current.minScore, object.score);
    }
    return [...groups.values()].sort(
      (a, b) => a.object.track_id - b.object.track_id,
    );
  }, [objects]);

  const promptValues = useMemo(() => parsePrompts(promptText), [promptText]);
  const selectedCameras = useMemo(() => [...runCameras].sort(), [runCameras]);
  const planFingerprint = useMemo(
    () =>
      JSON.stringify({
        episodes: scopeEpisodes,
        cameras: selectedCameras,
        prompts: promptValues,
      }),
    [promptValues, scopeEpisodes, selectedCameras],
  );
  useEffect(() => {
    setPlanState("idle");
    setPlanId(null);
  }, [planFingerprint]);

  const runSam3Annotation = async () => {
    const plan = {
      episode_indices: scopeEpisodes,
      camera_keys: selectedCameras,
      prompts: promptValues,
      start_frame: 0,
      max_frames: null,
      review_threshold: 0.6,
      accept_threshold: 0.9,
      provider: "sam3" as const,
    };
    if (
      !plan.prompts.length ||
      !plan.camera_keys.length ||
      !plan.episode_indices.length
    )
      return;
    setBusy(true);
    setPlanState("checking");
    setMessage(null);
    setErrorDetail(null);
    setItemErrors([]);
    setJobProgress(null);
    try {
      const staged = await planSam3(stableIdent, plan);
      setPlanId(staged.plan_id);
      setPlanState("ready");
      setMessage(
        language === "zh"
          ? "计划已校验：" +
              plan.episode_indices.length +
              " 个片段 × " +
              plan.camera_keys.length +
              " 个相机；正在启动 SAM3…"
          : "Plan validated: " +
              plan.episode_indices.length +
              " episode(s) × " +
              plan.camera_keys.length +
              " camera(s); starting SAM3…",
      );
      const result = await runSam3(stableIdent, {
        ...plan,
        plan_id: staged.plan_id,
      });
      if (!result.job_id) throw new Error("SAM3 did not return a job ID");
      setJobId(result.job_id);
      setMessage(
        language === "zh"
          ? "SAM3 作业已启动，正在准备模型和标注…"
          : "SAM3 job started; preparing the model and annotations…",
      );
      // Scale the client wait with the number of independent
      // (episode, camera) items. Status is already refreshed by the global
      // five-second timer, so avoid making an auth request every second.
      const maxAttempts = Math.max(
        180,
        plan.episode_indices.length * plan.camera_keys.length * 12,
      );
      for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const job = await fetchSam3Job(result.job_id, stableIdent);
        if (attempt % 5 === 0) await refreshStatus();
        setJobProgress(job.progress ?? null);
        if (job.status === "succeeded") {
          setItemErrors(job.item_errors ?? []);
          setMessage(
            job.item_errors?.length
              ? language === "zh"
                ? "SAM3 建议已保存（" +
                  job.item_errors.length +
                  " 个片段/相机组合失败，见下方详情）"
                : "SAM3 suggestions saved (" +
                  job.item_errors.length +
                  " episode/camera pair(s) failed — see details below)"
              : language === "zh"
                ? "SAM3 建议已保存，请审核每条轨迹后再导出"
                : "SAM3 suggestions saved; review every track before export",
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

  const downloadCheckpoint = async () => {
    setDownloadBusy(true);
    setMessage(null);
    try {
      const result = await startSam3CheckpointDownload();
      setCapabilities(result);
      setMessage(
        language === "zh"
          ? result.download_started
            ? "Checkpoint 下载已启动；状态卡会持续更新进度。"
            : result.checkpoint_cached
              ? "Checkpoint 已存在于当前工作区。"
              : "Checkpoint 下载正在进行中。"
          : result.download_started
            ? "Checkpoint download started; progress will update in the status card."
            : result.checkpoint_cached
              ? "The checkpoint is already available in this workspace."
              : "Checkpoint download is already in progress.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
      await refreshStatus();
    } finally {
      setDownloadBusy(false);
    }
  };

  const applyPreset = (name: string) => {
    const preset = presets.find((item) => item.name === name);
    if (preset) setPromptText(preset.prompts.join(", "));
  };

  const saveCurrentAsPreset = async () => {
    const name = presetNameDraft.trim();
    const prompts = parsePrompts(promptText);
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
  const checkpointReady = !!capabilities?.checkpoint_cached;
  const hubReady = !!account?.authenticated;
  const accountReady = hubReady || checkpointReady;
  const runtimeReady = workerReady && checkpointReady;
  const checkpointDownloadAvailable =
    capabilities?.checkpoint_download_available !== false;
  const downloadActive =
    download?.phase === "downloading" ||
    !!capabilities?.checkpoint_download_in_progress;
  const downloadCanStart =
    hubReady &&
    checkpointDownloadAvailable &&
    !downloadActive &&
    !downloadBusy &&
    !checkpointReady;
  const reviewCounts = useMemo(
    () =>
      trackSummaries.reduce(
        (counts, track) => {
          counts.total += 1;
          counts[track.object.status] += 1;
          return counts;
        },
        {
          total: 0,
          suggested: 0,
          accepted: 0,
          rejected: 0,
          needs_review: 0,
        } as Record<string, number>,
      ),
    [trackSummaries],
  );

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
          <div>
            <strong>
              <T>SAM3 runtime</T>
            </strong>
            <span className="object-annotation-status-subtitle">
              <T>Global worker · LeRobot sidecar workflow</T>
            </span>
          </div>
          <span className={runtimeReady ? "ready" : "muted"}>
            {runtimeReady ? <T>Ready</T> : <T>Setup required</T>}
          </span>
        </div>
        <div className="object-annotation-gates" aria-label="SAM3 setup gates">
          <span className={accountReady ? "ready" : "muted"}>
            <i aria-hidden="true">{accountReady ? "✓" : "1"}</i>
            <T>Hub access</T>
          </span>
          <span className={workerReady ? "ready" : "muted"}>
            <i aria-hidden="true">{workerReady ? "✓" : "2"}</i>
            <T>CUDA worker</T>
          </span>
          <span className={checkpointReady ? "ready" : "muted"}>
            <i aria-hidden="true">{checkpointReady ? "✓" : "3"}</i>
            <T>Checkpoint</T>
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
          <span className={accountReady ? "ready" : "muted"}>
            {account?.authenticated ? (
              account.username || (oauth ? "signed in" : "environment account")
            ) : checkpointReady ? (
              <T>Checkpoint cached</T>
            ) : (
              <T>not signed in</T>
            )}
          </span>
          {!account?.authenticated && !checkpointReady && (
            <HfAuthButton variant="ghost" />
          )}
          <span>
            <T>Checkpoint location</T>
          </span>
          <code className="object-annotation-path">
            {capabilities?.checkpoint_path ||
              "workspace/checkpoints/sam3/sam3.pt"}
          </code>
        </div>
        {checkpointReady && download?.phase === "ready" && (
          <p className="object-annotation-runtime">
            <span className="ready">
              <T>Checkpoint ready</T>
            </span>
            <span>{formatBytes(download.bytes)}</span>
          </p>
        )}
        {!checkpointReady && capabilities?.enabled && (
          <div className="object-annotation-checkpoint-card">
            <div className="object-annotation-checkpoint-head">
              <div>
                <strong>
                  <T>SAM3 checkpoint required</T>
                </strong>
                <span>
                  {hubReady ? (
                    <T>
                      Your Hugging Face session is ready. Download the
                      checkpoint once; future datasets reuse this workspace
                      copy.
                    </T>
                  ) : (
                    <T>
                      Sign in to Hugging Face, then download the checkpoint into
                      this workspace before running SAM3.
                    </T>
                  )}
                </span>
              </div>
              <button
                type="button"
                className="object-annotation-download"
                onClick={() => void downloadCheckpoint()}
                disabled={!downloadCanStart}
              >
                {downloadActive ? (
                  <T>Downloading…</T>
                ) : download?.phase === "error" ? (
                  <T>Retry download</T>
                ) : (
                  <T>Download checkpoint</T>
                )}
              </button>
            </div>
            <div className="object-annotation-progress">
              <div className="object-annotation-progress-label">
                <span>
                  {downloadActive ? (
                    <T>Downloading checkpoint</T>
                  ) : (
                    <T>Checkpoint is required before a real SAM3 run.</T>
                  )}
                </span>
                <span>
                  {downloadPercent === null
                    ? downloadActive
                      ? "…"
                      : "0.0%"
                    : downloadPercent.toFixed(1) + "%"}
                  {" · "}
                  {formatBytes(download?.bytes)}
                  {download?.total_bytes
                    ? " / " + formatBytes(download.total_bytes)
                    : ""}
                </span>
              </div>
              <progress
                max={100}
                value={
                  downloadActive && downloadPercent === null
                    ? undefined
                    : (downloadPercent ?? 0)
                }
                aria-label="SAM3 checkpoint download progress"
              />
            </div>
            {download?.phase === "error" && (
              <p className="object-annotation-runtime muted">
                {download.message || "Checkpoint download failed"}
              </p>
            )}
            {!hubReady && !checkpointReady && (
              <p className="object-annotation-checkpoint-hint">
                <T>Sign in first to enable checkpoint download.</T>
              </p>
            )}
            {!checkpointDownloadAvailable && (
              <p className="object-annotation-checkpoint-hint">
                <T>
                  Checkpoint download is disabled because LEVI_SAM3_CHECKPOINT
                  is set. Place the file at the path above.
                </T>
              </p>
            )}
          </div>
        )}
        {!accountReady && (
          <p className="object-annotation-runtime muted">
            <T>
              Sign in to Hugging Face or place an existing checkpoint in the
              workspace before starting a real SAM3 job.
            </T>
          </p>
        )}
        <p className="object-annotation-status-note">
          <T>
            SAM3 reads native LeRobot frames and writes lossless RLE masks to a
            separate sidecar. Track IDs are scoped to each episode and camera.
          </T>
        </p>
      </div>

      <div className="object-annotation-step">
        <div className="object-annotation-step-head">
          <span className="object-annotation-step-number">01</span>
          <div>
            <strong>
              <T>Define object prompts</T>
            </strong>
            <span>
              <T>
                Use text concepts for SAM3 to detect and track in the selected
                camera.
              </T>
            </span>
          </div>
        </div>
        <div className="object-annotation-controls">
          <label>
            <span>
              <T>Review camera</T>
            </span>
            <select
              value={cameraKey}
              onChange={(event) => setCameraKey(event.target.value)}
              disabled={!cameraKeys.length || busy}
            >
              {cameraKeys.map((key) => (
                <option key={key} value={key}>
                  {key}
                </option>
              ))}
            </select>
          </label>
          <label className="object-annotation-prompt">
            <span>
              <T>Text prompts (comma, semicolon or newline separated)</T>
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
                {language === "zh"
                  ? "加载 prompt 预设…"
                  : "Load prompt preset…"}
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
        </div>
      </div>

      <div className="object-annotation-step">
        <div className="object-annotation-step-head">
          <span className="object-annotation-step-number">02</span>
          <div>
            <strong>
              <T>Choose annotation scope</T>
            </strong>
            <span>
              <T>
                Each episode/camera pair is processed independently so native
                frame indices stay aligned.
              </T>
            </span>
          </div>
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
                    setScope({
                      kind: "task",
                      task: taskIndex?.tasks[0] ?? "",
                    });
                  else
                    setScope({
                      kind: "range",
                      from: episodeId,
                      to: episodeId,
                    });
                }}
                disabled={busy}
              >
                <option value="range">
                  {language === "zh" ? "片段范围" : "Episode range"}
                </option>
                {!!taskIndex?.tasks.length && (
                  <option value="task">
                    {language === "zh" ? "按任务筛选" : "By task"}
                  </option>
                )}
                <option value="all">
                  {language === "zh" ? "全部片段" : "All episodes"}
                </option>
              </select>
            </label>
            {scope.kind === "range" && (
              <div className="object-annotation-range">
                <label>
                  <span>
                    <T>From</T>
                  </span>
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
                    aria-label="First episode"
                  />
                </label>
                <span className="object-annotation-range-dash">–</span>
                <label>
                  <span>
                    <T>To</T>
                  </span>
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
                    aria-label="Last episode"
                  />
                </label>
              </div>
            )}
            {scope.kind === "task" && (
              <label className="object-annotation-task-select">
                <span>
                  <T>Task</T>
                </span>
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
              </label>
            )}
            <span className="object-annotation-scope-count">
              {scopeEpisodes.length} <T>episode(s) selected</T>
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
          <div className="object-annotation-selection-summary">
            <span>
              {scopeEpisodes.length} <T>episode(s) selected</T>
            </span>
            <span>·</span>
            <span>
              {selectedCameras.length} <T>camera(s) selected</T>
            </span>
          </div>
        </div>
      </div>

      <div className="object-annotation-runbar">
        <div className="object-annotation-plan-state">
          <span className={planState === "ready" ? "ready" : "muted"}>
            {planState === "checking" ? (
              <T>Validating plan…</T>
            ) : planState === "ready" ? (
              <T>Plan validated</T>
            ) : (
              <T>Plan not validated</T>
            )}
          </span>
          {planId && <code>{planId.slice(0, 12)}</code>}
          {!runtimeReady && (
            <small>
              <T>Complete Hub access, worker and checkpoint setup above.</T>
            </small>
          )}
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
              !selectedCameras.length ||
              !scopeEpisodes.length ||
              !promptValues.length ||
              !runtimeReady
            }
            title="Uses the configured CUDA worker and 1038lab/sam3 checkpoint"
          >
            <T>Run SAM3 annotation</T>
          </button>
        )}
      </div>

      {busy && jobProgress && (
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
      {!trackSummaries.length ? (
        <p className="object-annotation-empty">
          <T>No object suggestions for this camera yet.</T>
        </p>
      ) : (
        <>
          <div className="object-annotation-review-summary">
            <strong>
              <T>Tracks in this camera</T>
            </strong>
            <span>
              {reviewCounts.total} <T>track(s)</T>
            </span>
            <span className="ready">
              {reviewCounts.accepted} <T>accepted</T>
            </span>
            <span className="muted">
              {reviewCounts.suggested + reviewCounts.needs_review}{" "}
              <T>to review</T>
            </span>
            <span className="rejected">
              {reviewCounts.rejected} <T>rejected</T>
            </span>
          </div>
          <div className="object-annotation-list">
            {trackSummaries.map((summary) => {
              const object = summary.object;
              return (
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
                      {(summary.meanScore * 100).toFixed(0)}% <T>mean</T>
                    </span>
                    <span className="object-frame">
                      f{summary.startFrame}–{summary.endFrame} ·{" "}
                      {summary.frameCount} <T>frames</T>
                    </span>
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
              );
            })}
          </div>
        </>
      )}
    </section>
  );
}
