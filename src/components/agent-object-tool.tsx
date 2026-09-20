"use client";
import { useEffect, useRef, useState } from "react";
import type { ObjectAnnotation } from "@/types/object-annotation.types";
import { T, useLocale } from "./levi-locale";

type Job = { id: string; status: string; count?: number; reason?: string };
type Frame = {
  problem_offsets: number[];
  frames: number;
  pending: number;
  revision: string;
  rows: ObjectAnnotation[];
  evidence?: {
    artifact: string;
    episode_index: number;
    camera_key: string;
    frame_index: number;
    timestamp: number;
  };
};
async function call<V>(name: string, args: unknown): Promise<V> {
  const response = await fetch("/api/levi/agent/v1/tools", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, arguments: args }),
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      typeof result.detail === "string" ? result.detail : "Object tool failed",
    );
  return result;
}
function Mask({ row }: { row: ObjectAnnotation }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const ctx = canvas.current?.getContext("2d");
    if (!ctx) return;
    const [h, w] = row.mask_rle.size;
    const pixels = ctx.createImageData(w, h);
    let offset = 0,
      foreground = false;
    for (const length of row.mask_rle.counts) {
      if (foreground)
        for (let i = offset; i < offset + length; i++) {
          const p = ((i % h) * w + Math.floor(i / h)) * 4;
          pixels.data.set([190, 232, 85, 105], p);
        }
      offset += length;
      foreground = !foreground;
    }
    ctx.putImageData(pixels, 0, 0);
    ctx.strokeStyle = "#bee855";
    ctx.lineWidth = 2;
    const [x1, y1, x2, y2] = row.bbox_xyxy;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
  }, [row]);
  return (
    <canvas
      ref={canvas}
      width={row.image_size[1]}
      height={row.image_size[0]}
      aria-label="Object mask overlay"
    />
  );
}
export default function AgentObjectTool({
  runId,
  status,
  jobIds,
  refresh,
}: {
  runId: string;
  status: string;
  jobIds: string[];
  refresh: () => void;
}) {
  const { t } = useLocale();
  const [ready, setReady] = useState(false),
    [prompts, setPrompts] = useState(""),
    [limit, setLimit] = useState(100);
  const [job, setJob] = useState<Job | null>(null),
    [frame, setFrame] = useState<Frame | null>(null),
    [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [label, setLabel] = useState("");
  const [maskOptions, setMaskOptions] = useState<{
    opacity: number;
    outline: boolean;
    hidden: number[];
  }>({ opacity: 1, outline: false, hidden: [] });
  function display(next: typeof maskOptions) {
    setMaskOptions(next);
    window.dispatchEvent(
      new CustomEvent("levi-agent-mask-options", { detail: next }),
    );
  }
  async function inspect(id: string, pos: number) {
    setFrame(await call<Frame>("objects.inspect", { job_id: id, offset: pos }));
  }
  useEffect(() => {
    let cancelled = false;
    void call<{ available: boolean }>("objects.status", {})
      .then((r) => {
        if (!cancelled) setReady(r.available);
      })
      .catch((e) => setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [runId]);
  useEffect(() => {
    const id = job?.id || jobIds.at(-1);
    if (!id) return;
    let cancelled = false;
    const check = async () => {
      try {
        const next = await call<Job>("objects.get", { job_id: id });
        if (!cancelled) setJob(next);
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    };
    void check();
    const timer = setInterval(check, 2500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [job?.id, jobIds]);
  useEffect(() => {
    if (job?.status === "waiting_for_review")
      void inspect(job.id, offset).catch((e) => setError(String(e)));
  }, [job?.id, job?.status, offset]);
  async function act(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function edit(operation: string, row?: ObjectAnnotation) {
    if (!job || !frame?.evidence) return;
    const e = frame.evidence;
    await call("objects.edit", {
      job_id: job.id,
      edit: {
        episode_index: e.episode_index,
        camera_key: e.camera_key,
        operation,
        base_revision: frame.revision,
        ...(row ? { object_id: row.object_id } : {}),
        ...(operation === "relabel" ? { concept: label } : {}),
      },
    });
    await inspect(job.id, offset);
  }
  const disabled =
    busy || ["succeeded", "partially_succeeded", "cancelled"].includes(status);
  return (
    <T>
      <details className="levi-agent-object">
        <summary>SAM3 · optional object tool</summary>
        <p>
          Use SAM3 when the task needs masks or tracks. Language review works
          independently.
        </p>
        <p role="status">
          {t(
            ready
              ? "Worker and local checkpoint ready"
              : "SAM3 unavailable. Configure the worker and checkpoint in the annotation settings.",
          )}
        </p>
        {error && <p role="alert">{error}</p>}
        <label>
          Object concepts
          <input
            value={prompts}
            onChange={(e) => setPrompts(e.target.value)}
            placeholder={t("cup, plate, robot gripper")}
          />
        </label>
        <label>
          Maximum frames per camera
          <input
            type="number"
            min={1}
            max={10000}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
          />
        </label>
        <button
          disabled={
            disabled ||
            !prompts.trim() ||
            ["running", "queued"].includes(status)
          }
          onClick={() =>
            void act(async () => {
              await call("runs.prepare", { run_id: runId });
              const next = await call<Job>("objects.plan", {
                run_id: runId,
                prompts: prompts
                  .split(",")
                  .map((p) => p.trim())
                  .filter(Boolean),
                max_frames: limit,
              });
              setJob(next);
              setFrame(null);
              setOffset(0);
            })
          }
        >
          Plan object assistance
        </button>
        {job && (
          <>
            <p>
              {t(job.status)} · {job.count ?? 0} <T>masks</T>
            </p>
            {job.reason && <p>{job.reason}</p>}
            {job.status === "planned" && (
              <button
                disabled={disabled || !ready}
                onClick={() =>
                  void act(async () => {
                    await call("objects.run", { job_id: job.id });
                    setJob({ ...job, status: "queued" });
                  })
                }
              >
                Run SAM3 on this scope
              </button>
            )}
          </>
        )}
        {frame && (
          <>
            <p>
              {frame.pending} <T>masks awaiting review</T> ·{" "}
              {Math.min(offset + 1, frame.frames)}/{frame.frames}
            </p>
            <div className="levi-agent-actions">
              <button
                disabled={offset === 0}
                onClick={() => setOffset((v) => v - 1)}
              >
                Previous frame
              </button>
              <button
                disabled={offset + 1 >= frame.frames}
                onClick={() => setOffset((v) => v + 1)}
              >
                Next frame
              </button>
            </div>
            <button
              disabled={!frame.problem_offsets?.length}
              onClick={() =>
                setOffset(
                  frame.problem_offsets.find((i) => i > offset) ??
                    frame.problem_offsets[0],
                )
              }
            >
              Next uncertain frame
            </button>
            <input
              aria-label={t("Object frame")}
              type="range"
              min={0}
              max={Math.max(0, frame.frames - 1)}
              value={offset}
              onChange={(e) => setOffset(Number(e.target.value))}
            />
            {frame.evidence && (
              <>
                <p>
                  {frame.evidence.camera_key} ·{" "}
                  {frame.evidence.timestamp.toFixed(3)}s
                </p>
                <div className="levi-agent-mask-frame">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={`/api/levi/agent/v1/runs/${runId}/artifacts/${frame.evidence.artifact}`}
                    alt={t("Frozen source frame")}
                  />
                  {frame.rows
                    .filter((r) => r.status !== "rejected")
                    .map((r) => (
                      <Mask key={r.object_id} row={r} />
                    ))}
                </div>
              </>
            )}
            <label>
              Mask opacity
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={maskOptions.opacity}
                onChange={(e) =>
                  display({ ...maskOptions, opacity: Number(e.target.value) })
                }
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={maskOptions.outline}
                onChange={(e) =>
                  display({ ...maskOptions, outline: e.target.checked })
                }
              />
              Mask contours only
            </label>
            {frame.rows.map((row) => (
              <label key={row.track_id}>
                <input
                  type="checkbox"
                  checked={!maskOptions.hidden.includes(row.track_id)}
                  onChange={(e) =>
                    display({
                      ...maskOptions,
                      hidden: e.target.checked
                        ? maskOptions.hidden.filter((id) => id !== row.track_id)
                        : [...maskOptions.hidden, row.track_id],
                    })
                  }
                />
                {row.concept} #{row.track_id}
              </label>
            ))}
            <div className="levi-agent-actions">
              <button
                onClick={() => {
                  if (!job) return;
                  void call<{ context: { repo_id: string } }>("runs.get", {
                    run_id: runId,
                  }).then((run) =>
                    window.dispatchEvent(
                      new CustomEvent("levi-agent-preview", {
                        detail: {
                          job_id: job.id,
                          repo_id: run.context.repo_id,
                          episode: frame.evidence?.episode_index,
                        },
                      }),
                    ),
                  );
                }}
              >
                Preview draft masks in current player
              </button>
              <button
                onClick={() =>
                  window.dispatchEvent(
                    new CustomEvent("levi-agent-preview", { detail: null }),
                  )
                }
              >
                Show published masks
              </button>
            </div>
            <p>
              Track decisions apply across its frames in this camera. Existing
              published objects are preserved.
            </p>
            {frame.rows.map((row) => (
              <article key={row.object_id}>
                <strong>
                  {row.concept} · #{row.track_id}
                </strong>
                <p>
                  {row.score.toFixed(2)} · {t(row.status)}
                </p>
                <div className="levi-agent-actions">
                  <button
                    disabled={disabled}
                    onClick={() => void act(() => edit("accept", row))}
                  >
                    Accept track
                  </button>
                  <button
                    disabled={disabled}
                    onClick={() => void act(() => edit("reject", row))}
                  >
                    Reject track
                  </button>
                  <button
                    disabled={disabled || !label.trim()}
                    onClick={() => void act(() => edit("relabel", row))}
                  >
                    Relabel track
                  </button>
                </div>
              </article>
            ))}
            <label>
              Corrected concept
              <input value={label} onChange={(e) => setLabel(e.target.value)} />
            </label>
            <div className="levi-agent-actions">
              <button
                disabled={disabled}
                onClick={() => void act(() => edit("accept"))}
              >
                Accept camera tracks
              </button>
              <button
                disabled={disabled}
                onClick={() => void act(() => edit("reject"))}
              >
                Reject camera tracks
              </button>
            </div>
          </>
        )}
      </details>
    </T>
  );
}
