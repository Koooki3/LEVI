"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import HfAuthButton from "@/components/hf-auth-button";
import { T, useLocale } from "@/components/levi-locale";
import { useAuth } from "@/context/auth-context";
import { useTime } from "@/context/time-context";
import {
  editObjectAnnotation,
  fetchObjectAnnotations,
  fetchSam3Job,
  getSam3Status,
  runSam3,
  type DatasetIdent,
} from "@/utils/annotationsClient";
import type {
  ObjectAnnotation,
  Sam3Capabilities,
} from "@/types/object-annotation.types";

type Props = {
  episodeId: number;
  ident: DatasetIdent;
  cameraKeys: string[];
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

  useEffect(() => {
    setCameraKey((current) =>
      cameraKeys.includes(current) ? current : cameraKeys[0] || "",
    );
  }, [cameraKeys]);

  useEffect(() => {
    setObjects([]);
    setRevision(null);
    setMessage(null);
  }, [stableIdent]);

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

  const runSam3Annotation = async () => {
    const prompts = promptText
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    if (!prompts.length || !cameraKey) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await runSam3(stableIdent, {
        episode_indices: [episodeId],
        camera_keys: [cameraKey],
        prompts,
        start_frame: 0,
        max_frames: null,
        review_threshold: 0.6,
        accept_threshold: 0.9,
        provider: "sam3",
      });
      if (!result.job_id) throw new Error("SAM3 did not return a job ID");
      setMessage(
        language === "zh"
          ? "SAM3 作业已启动，正在准备模型和标注…"
          : "SAM3 job started; preparing the model and annotations…",
      );
      for (let attempt = 0; attempt < 180; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        const job = await fetchSam3Job(result.job_id, stableIdent);
        await refreshStatus();
        if (job.status === "succeeded") {
          setMessage(
            language === "zh"
              ? "SAM3 建议已保存，请开始审核"
              : "SAM3 suggestions saved; review them now",
          );
          break;
        }
        if (job.status === "failed" || job.status === "cancelled") {
          throw new Error(String(job.error || "SAM3 job " + job.status));
        }
        if (attempt === 179) throw new Error("SAM3 job timed out");
      }
      await refresh();
      window.dispatchEvent(new Event("levi:sam3-updated"));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
      void refreshStatus();
    }
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
        <button
          type="button"
          className="object-annotation-run sam3"
          onClick={() => void runSam3Annotation()}
          disabled={busy || !cameraKey || !promptText.trim() || !workerReady}
          title="Uses the configured CUDA worker and 1038lab/sam3 checkpoint"
        >
          {busy ? <T>Working…</T> : <T>Run SAM3 annotation</T>}
        </button>
      </div>

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
