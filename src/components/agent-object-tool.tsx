"use client";
import { useEffect, useId, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Eye,
  EyeOff,
  Play,
  ScanSearch,
  Shapes,
  TriangleAlert,
  X,
} from "lucide-react";
import type { ObjectAnnotation } from "@/types/object-annotation.types";
import { T, useLocale } from "./levi-locale";
import { Badge, Button, Card, Checkbox, Field, Input } from "@/components/ds";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { Actions, Disclosure, GatedButton, Hint } from "./agent-ui";

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
/** "rgb(0, 131, 0)" → [0, 131, 0]; null for anything else. */
export function parseRgb(value: string): [number, number, number] | null {
  const m = value.match(/^rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

function Mask({ row }: { row: ObjectAnnotation }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const ctx = canvas.current?.getContext("2d");
    if (!ctx || !canvas.current) return;
    // The mask colour is a data colour (--ds-data-6, set as the canvas's
    // CSS colour in agent-content.css): canvas cannot read CSS variables,
    // so the resolved value is taken from the element.
    const colour = getComputedStyle(canvas.current).color;
    const [r, g, b] = parseRgb(colour) ?? [0, 131, 0];
    const [h, w] = row.mask_rle.size;
    const pixels = ctx.createImageData(w, h);
    let offset = 0,
      foreground = false;
    for (const length of row.mask_rle.counts) {
      if (foreground)
        for (let i = offset; i < offset + length; i++) {
          const p = ((i % h) * w + Math.floor(i / h)) * 4;
          pixels.data.set([r, g, b, 105], p);
        }
      offset += length;
      foreground = !foreground;
    }
    ctx.putImageData(pixels, 0, 0);
    ctx.strokeStyle = `rgb(${r} ${g} ${b})`;
    ctx.lineWidth = 2;
    const [x1, y1, x2, y2] = row.bbox_xyxy;
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
  }, [row]);
  return (
    <canvas
      ref={canvas}
      className="ag-mask"
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
  const closed = ["succeeded", "partially_succeeded", "cancelled"].includes(
    status,
  );
  const relabelWhy = useId();
  const closedWhy = useId();
  const planReason = closed
    ? t("This task is finished, so no new object job can be planned.")
    : ["running", "queued"].includes(status)
      ? t("Wait until the task stops running.")
      : !prompts.trim()
        ? t("Enter at least one object concept.")
        : null;
  return (
    <T>
      <Disclosure icon={Shapes} summary={t("SAM3 · optional object tool")}>
        <div className="ag-stack">
          <p>
            Use SAM3 when the task needs masks or tracks. Language review works
            independently.
          </p>
          <Note tone={ready ? "success" : "warning"} role="status">
            {t(
              ready
                ? "Worker and local checkpoint ready"
                : "SAM3 unavailable. Configure the worker and checkpoint in the annotation settings.",
            )}
          </Note>
          {error && (
            <RequestProblem
              action="The object tool request failed"
              message={error}
            />
          )}
          <Field label={t("Object concepts")}>
            <Input
              value={prompts}
              onChange={(e) => setPrompts(e.target.value)}
              placeholder={t("cup, plate, robot gripper")}
            />
          </Field>
          <Field label={t("Maximum frames per camera")}>
            <Input
              type="number"
              min={1}
              max={10000}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            />
          </Field>
          <Actions>
            <GatedButton
              size="sm"
              icon={ScanSearch}
              disabled={busy}
              reason={planReason}
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
              {t("Plan object assistance")}
            </GatedButton>
          </Actions>
          {job && (
            <>
              <p>
                <Badge tone="neutral">{t(job.status)}</Badge> {job.count ?? 0}{" "}
                <T>masks</T>
              </p>
              {job.reason && <p>{job.reason}</p>}
              {job.status === "planned" && (
                <Actions>
                  <GatedButton
                    size="sm"
                    icon={Play}
                    disabled={disabled}
                    reason={
                      !ready
                        ? t("SAM3 is not ready; see the note above.")
                        : null
                    }
                    onClick={() =>
                      void act(async () => {
                        await call("objects.run", { job_id: job.id });
                        setJob({ ...job, status: "queued" });
                      })
                    }
                  >
                    {t("Run SAM3 on this scope")}
                  </GatedButton>
                </Actions>
              )}
            </>
          )}
          {frame && (
            <>
              <p>
                {frame.pending} <T>masks awaiting review</T> ·{" "}
                {Math.min(offset + 1, frame.frames)}/{frame.frames}
              </p>
              <Actions>
                <Button
                  size="sm"
                  icon={ArrowLeft}
                  disabled={offset === 0}
                  onClick={() => setOffset((v) => v - 1)}
                >
                  {t("Previous frame")}
                </Button>
                <Button
                  size="sm"
                  iconEnd={ArrowRight}
                  disabled={offset + 1 >= frame.frames}
                  onClick={() => setOffset((v) => v + 1)}
                >
                  {t("Next frame")}
                </Button>
                <GatedButton
                  size="sm"
                  icon={TriangleAlert}
                  reason={
                    !frame.problem_offsets?.length
                      ? t("No frame is marked uncertain.")
                      : null
                  }
                  onClick={() =>
                    setOffset(
                      frame.problem_offsets.find((i) => i > offset) ??
                        frame.problem_offsets[0],
                    )
                  }
                >
                  {t("Next uncertain frame")}
                </GatedButton>
              </Actions>
              <Input
                aria-label={t("Object frame")}
                className="ag-range"
                type="range"
                min={0}
                max={Math.max(0, frame.frames - 1)}
                value={offset}
                onChange={(e) => setOffset(Number(e.target.value))}
              />
              {frame.evidence && (
                <>
                  <p className="ag-muted">
                    {frame.evidence.camera_key} ·{" "}
                    {frame.evidence.timestamp.toFixed(3)}s
                  </p>
                  <div className="ag-mask-frame">
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
              <Field label={t("Mask opacity")}>
                <Input
                  className="ag-range"
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={maskOptions.opacity}
                  onChange={(e) =>
                    display({
                      ...maskOptions,
                      opacity: Number(e.target.value),
                    })
                  }
                />
              </Field>
              <Checkbox
                label={t("Mask contours only")}
                checked={maskOptions.outline}
                onChange={(e) =>
                  display({ ...maskOptions, outline: e.target.checked })
                }
              />
              {frame.rows.map((row) => (
                <Checkbox
                  key={row.track_id}
                  label={`${row.concept} #${row.track_id}`}
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
              ))}
              <Actions>
                <Button
                  size="sm"
                  icon={Eye}
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
                  {t("Preview draft masks in current player")}
                </Button>
                <Button
                  size="sm"
                  icon={EyeOff}
                  onClick={() =>
                    window.dispatchEvent(
                      new CustomEvent("levi-agent-preview", { detail: null }),
                    )
                  }
                >
                  {t("Show published masks")}
                </Button>
              </Actions>
              <Hint>
                Track decisions apply across its frames in this camera. Existing
                published objects are preserved.
              </Hint>
              {closed && (
                <Hint id={closedWhy}>
                  {t("This task is finished, so decisions are closed.")}
                </Hint>
              )}
              <Field label={t("Corrected concept")}>
                <Input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                />
              </Field>
              {!label.trim() && (
                <Hint id={relabelWhy}>
                  {t("Enter the corrected concept to enable Relabel track.")}
                </Hint>
              )}
              {frame.rows.map((row) => (
                <Card
                  key={row.object_id}
                  padding="compact"
                  title={`${row.concept} · #${row.track_id}`}
                  actions={<Badge tone="neutral">{t(row.status)}</Badge>}
                >
                  <p className="ag-muted">{row.score.toFixed(2)}</p>
                  <Actions>
                    <Button
                      size="sm"
                      icon={Check}
                      disabled={disabled}
                      aria-describedby={closed ? closedWhy : undefined}
                      onClick={() => void act(() => edit("accept", row))}
                    >
                      {t("Accept track")}
                    </Button>
                    <Button
                      size="sm"
                      icon={X}
                      disabled={disabled}
                      aria-describedby={closed ? closedWhy : undefined}
                      onClick={() => void act(() => edit("reject", row))}
                    >
                      {t("Reject track")}
                    </Button>
                    <Button
                      size="sm"
                      disabled={disabled || !label.trim()}
                      aria-describedby={
                        closed
                          ? closedWhy
                          : !label.trim()
                            ? relabelWhy
                            : undefined
                      }
                      onClick={() => void act(() => edit("relabel", row))}
                    >
                      {t("Relabel track")}
                    </Button>
                  </Actions>
                </Card>
              ))}
              <Actions>
                <Button
                  size="sm"
                  icon={Check}
                  disabled={disabled}
                  aria-describedby={closed ? closedWhy : undefined}
                  onClick={() => void act(() => edit("accept"))}
                >
                  {t("Accept camera tracks")}
                </Button>
                <Button
                  size="sm"
                  icon={X}
                  disabled={disabled}
                  aria-describedby={closed ? closedWhy : undefined}
                  onClick={() => void act(() => edit("reject"))}
                >
                  {t("Reject camera tracks")}
                </Button>
              </Actions>
            </>
          )}
        </div>
      </Disclosure>
    </T>
  );
}
