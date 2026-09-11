"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { T, useLocale } from "@/components/levi-locale";
import { useTime } from "@/context/time-context";
import {
  editObjectAnnotation,
  fetchObjectAnnotations,
  fetchSam3Job,
  getSam3Capabilities,
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

export default function ObjectAnnotationPanel({
  episodeId,
  ident,
  cameraKeys,
}: Props) {
  const { language } = useLocale();
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

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    getSam3Capabilities()
      .then(setCapabilities)
      .catch(() => setCapabilities(null));
  }, []);

  const tracks = useMemo(() => {
    const values = new Map<string, ObjectAnnotation>();
    for (const object of objects) {
      const key = `${object.object_id}:${object.track_id}`;
      if (!values.has(key)) values.set(key, object);
    }
    return [...values.values()].sort((a, b) => a.track_id - b.track_id);
  }, [objects]);

  const runProvider = async (provider: "fake" | "sam3") => {
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
        max_frames: provider === "fake" ? 24 : null,
        review_threshold: 0.6,
        accept_threshold: 0.9,
        provider,
      });
      if (provider === "fake") {
        setMessage(
          language === "zh"
            ? `CPU 演示已生成 ${result.count ?? 0} 条建议`
            : `CPU demo created ${result.count ?? 0} suggestions`,
        );
      } else if (result.job_id) {
        setMessage(
          language === "zh" ? "SAM3 作业运行中…" : "SAM3 job is running…",
        );
        for (let attempt = 0; attempt < 180; attempt += 1) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          const job = await fetchSam3Job(result.job_id, ident);
          if (job.status === "succeeded") {
            setMessage(
              language === "zh"
                ? "SAM3 建议已保存，请开始审核"
                : "SAM3 suggestions saved; review them now",
            );
            break;
          }
          if (job.status === "failed" || job.status === "cancelled") {
            throw new Error(String(job.error || `SAM3 job ${job.status}`));
          }
        }
      }
      await refresh();
      window.dispatchEvent(new Event("levi:sam3-updated"));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const runCpuDemo = () => void runProvider("fake");
  const runRealSam3 = () => void runProvider("sam3");

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
          className="object-annotation-run"
          onClick={runCpuDemo}
          disabled={busy || !cameraKey || !promptText.trim()}
        >
          {busy ? <T>Working…</T> : <T>CPU demo</T>}
        </button>
        <button
          type="button"
          className="object-annotation-run sam3"
          onClick={runRealSam3}
          disabled={
            busy ||
            !cameraKey ||
            !promptText.trim() ||
            !capabilities?.enabled ||
            !capabilities.worker_project_present ||
            !capabilities.worker_python_present
          }
          title="Requires the optional SAM3 uv environment"
        >
          <T>Run SAM3 worker</T>
        </button>
      </div>

      <div className="object-annotation-runtime">
        <span className={capabilities?.enabled ? "ready" : "muted"}>
          <T>
            {capabilities?.enabled
              ? "SAM3 worker enabled"
              : "SAM3 worker disabled"}
          </T>
        </span>
        <span>
          <T>CPU demo is deterministic and safe for local review.</T>
        </span>
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
              key={`${object.object_id}:${object.track_id}`}
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
