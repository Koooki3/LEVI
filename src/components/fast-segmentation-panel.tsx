"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FaBolt, FaStop, FaTrash } from "react-icons/fa";
import { T, useLocale } from "@/components/levi-locale";
import {
  cancelSam3Job,
  cancelSegJob,
  deleteSegModel,
  fetchSam3Job,
  fetchSegJob,
  fetchSegStatus,
  liveEventsUrl,
  planSam3,
  runSam3,
  sendLiveClock,
  startLiveSession,
  startSegDistil,
  startSegLabel,
  stopLiveSession,
  stopLiveSessionOnUnload,
  type DatasetIdent,
} from "@/utils/annotationsClient";
import {
  createCoalescer,
  parseEpisodeList,
  primaryLiveVideo,
  pushLiveResult,
  setLiveSession,
  subscribeLive,
} from "@/utils/liveSegmentation";
import type { Sam3JobStatus } from "@/types/object-annotation.types";
import type {
  LiveResult,
  LiveSessionInfo,
  LiveStats,
  SegItemError,
  SegJob,
  SegModel,
  SegStatus,
} from "@/types/segmentation.types";

type Props = {
  episodeId: number;
  ident: DatasetIdent;
  cameraKeys: string[];
  /** Every episode index in the dataset (the teacher needs explicit lists). */
  allEpisodes?: number[];
};

/** One offline run as the panel shows it, whichever engine runs it. */
type JobView = {
  id: string;
  engine: "student" | "teacher";
  status: string;
  stage?: string;
  done?: number;
  total?: number;
  error?: string | null;
  errorDetail?: string | null;
  revisionId?: string | null;
  annotationCount?: number | null;
  itemErrors?: SegItemError[];
  fps?: number | null;
  seconds?: number | null;
  model?: string | null;
};

const ACTIVE = new Set(["queued", "running", "planned", "pending", "starting"]);
const POLL_MS = 1500;
const DEFAULT_CONCEPTS =
  "pink plate, white plate, green plate, blue plate, cup, robot arm, human hand";
const ARCHITECTURES = [
  "rf-detr-seg-nano",
  "rf-detr-seg-small",
  "rf-detr-seg-medium",
];

function fromSegJob(job: SegJob): JobView {
  const timing = job.timing ?? {};
  return {
    id: job.id,
    engine: "student",
    status: job.status,
    stage: job.progress?.stage,
    done: job.progress?.done,
    total: job.progress?.total,
    error: job.error,
    errorDetail: job.error_detail,
    revisionId: job.revision_id,
    annotationCount: job.annotation_count,
    itemErrors: job.item_errors ?? [],
    fps: typeof timing.fps === "number" ? timing.fps : null,
    seconds: typeof timing.seconds === "number" ? timing.seconds : null,
    model: job.model,
  };
}

function fromSam3Job(id: string, job: Sam3JobStatus): JobView {
  return {
    id,
    engine: "teacher",
    status: job.status ?? "running",
    stage: job.progress?.current_camera
      ? `episode ${job.progress.current_episode ?? "?"} · ${job.progress.current_camera}`
      : undefined,
    done: job.progress?.done,
    total: job.progress?.total,
    error: job.error,
    errorDetail: job.error_detail,
    revisionId: job.revision_id,
    annotationCount: job.annotation_count,
    itemErrors: job.item_errors ?? [],
  };
}

function num(value: number | null | undefined, digits = 1): string {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toFixed(digits)
    : "–";
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function splitConcepts(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[,;\n]/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

/** Poll one job every POLL_MS while it is active. */
function useJobPoll(
  job: JobView | null,
  poll: (job: JobView) => Promise<JobView>,
  update: (job: JobView) => void,
) {
  const pollRef = useRef(poll);
  const updateRef = useRef(update);
  useEffect(() => {
    pollRef.current = poll;
    updateRef.current = update;
  });
  const id = job && ACTIVE.has(job.status) ? job.id : null;
  const engine = job?.engine;
  useEffect(() => {
    if (!id || !engine) return;
    let stopped = false;
    let timer = 0;
    const step = async () => {
      try {
        const next = await pollRef.current({ id, engine, status: "running" });
        if (stopped) return;
        updateRef.current(next);
        if (!ACTIVE.has(next.status)) return;
      } catch (error) {
        if (stopped) return;
        updateRef.current({
          id,
          engine,
          status: "running",
          error: message(error),
        });
      }
      timer = window.setTimeout(step, POLL_MS);
    };
    timer = window.setTimeout(step, POLL_MS);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [id, engine]);
}

function JobProgress({
  job,
  onCancel,
}: {
  job: JobView;
  onCancel: () => void;
}) {
  const { t } = useLocale();
  const active = ACTIVE.has(job.status);
  const total = job.total ?? 0;
  const done = job.done ?? 0;
  const percent = total > 0 ? Math.min(100, (100 * done) / total) : null;
  return (
    <T>
      {
        <div className="fast-seg-job">
          <div className="object-annotation-progress">
            <div className="object-annotation-progress-label">
              <span>
                <T>{job.status}</T>
                {job.stage ? " · " + job.stage : ""}
              </span>
              <span>
                {total > 0 ? `${done} / ${total}` : ""}
                {percent !== null ? ` · ${percent.toFixed(0)}%` : ""}
              </span>
            </div>
            <progress
              max={100}
              value={active && percent === null ? undefined : (percent ?? 100)}
              aria-label={t("Job progress")}
            />
          </div>
          {active && (
            <button
              type="button"
              className="object-annotation-run"
              onClick={onCancel}
            >
              <T>Cancel</T>
            </button>
          )}
          {job.status === "succeeded" && (
            <p className="object-annotation-runtime">
              <span className="ready">
                <T>Saved</T>
              </span>
              {job.annotationCount != null && (
                <span>
                  {job.annotationCount} <T>objects</T>
                </span>
              )}
              {job.revisionId && (
                <span>
                  <T>revision</T> <code>{job.revisionId.slice(0, 12)}</code>
                </span>
              )}
              {job.fps != null && (
                <span>
                  {num(job.fps)} <T>frames/s</T>
                </span>
              )}
              {job.seconds != null && <span>{num(job.seconds, 0)} s</span>}
            </p>
          )}
          {job.error && <p className="fast-seg-error">{job.error}</p>}
          {job.errorDetail && (
            <details className="object-annotation-error-detail">
              <summary>
                <T>Worker log</T>
              </summary>
              <pre>{job.errorDetail}</pre>
            </details>
          )}
          {!!job.itemErrors?.length && (
            <details className="object-annotation-error-detail">
              <summary>
                {job.itemErrors.length} <T>item(s) failed</T>
              </summary>
              <pre>
                {job.itemErrors
                  .map(
                    (item) =>
                      `${item.episode_index ?? "?"} / ${item.camera_key ?? "?"}: ${item.error ?? ""}`,
                  )
                  .join("\n")}
              </pre>
            </details>
          )}
        </div>
      }
    </T>
  );
}

function ModelCard({ model }: { model: SegModel }) {
  const metrics = model.metrics;
  const concepts = Object.entries(metrics?.concepts ?? {});
  return (
    <T>
      {
        <div className="fast-seg-model">
          <div className="fast-seg-grid">
            <span>
              <T>Concepts</T>
            </span>
            <span>{model.concepts.join(", ")}</span>
            <span>
              <T>Architecture</T>
            </span>
            <code>{model.architecture ?? "–"}</code>
            <span>
              <T>Held-out AP50 / AP vs teacher</T>
            </span>
            <span>
              {num(metrics?.ap50, 3)} / {num(metrics?.ap, 3)}
              {metrics?.images ? ` · ${metrics.images} ` : ""}
              {metrics?.images ? <T>images</T> : null}
            </span>
            <span>
              <T>Recall (any class)</T>
            </span>
            <span>{num(metrics?.recall_class_agnostic, 3)}</span>
            {model.teacher && (
              <>
                <span>
                  <T>Teacher model</T>
                </span>
                <span>{model.teacher}</span>
              </>
            )}
          </div>
          {concepts.length > 0 && (
            <div className="fast-seg-recall">
              <span>
                <T>Recall per concept</T>
              </span>
              {concepts.map(([concept, recall]) => (
                <span key={concept} className="fast-seg-chip">
                  {concept} <b>{num(recall, 2)}</b>
                </span>
              ))}
            </div>
          )}
          {model.licence && (
            <details className="object-annotation-error-detail">
              <summary>
                <T>Licence</T>
              </summary>
              <ul className="fast-seg-licence">
                {Object.entries(model.licence).map(([key, value]) => (
                  <li key={key}>
                    <code>{key}</code> {value}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      }
    </T>
  );
}

export default function FastSegmentationPanel({
  episodeId,
  ident,
  cameraKeys,
  allEpisodes,
}: Props) {
  const { t } = useLocale();
  const stableIdent = useMemo<DatasetIdent>(
    () => ({
      repoId: ident.repoId ?? null,
      localPath: ident.localPath ?? null,
      revision: ident.revision ?? null,
    }),
    [ident.localPath, ident.repoId, ident.revision],
  );
  const [status, setStatus] = useState<SegStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [modelName, setModelName] = useState("");

  // ------------------------------------------------------------ status
  const resumed = useRef(false);
  const refreshStatus = useCallback(async () => {
    try {
      const next = await fetchSegStatus(stableIdent);
      setStatus(next);
      setStatusError(null);
      setModelName((current) =>
        next.models.some((m) => m.name === current)
          ? current
          : (next.models[0]?.name ?? ""),
      );
      return next;
    } catch (error) {
      setStatusError(message(error));
      return null;
    }
  }, [stableIdent]);

  const model = status?.models.find((m) => m.name === modelName) ?? null;

  // ------------------------------------------------------------ live
  const [save, setSave] = useState(true);
  const [session, setSession] = useState<LiveSessionInfo | null>(null);
  const [liveBusy, setLiveBusy] = useState<"starting" | "stopping" | null>(
    null,
  );
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveNote, setLiveNote] = useState<string | null>(null);
  const [stats, setStats] = useState<LiveStats | null>(null);
  const sessionRef = useRef<string | null>(null);
  const streamRef = useRef<EventSource | null>(null);
  const identRef = useRef(stableIdent);
  useEffect(() => {
    identRef.current = stableIdent;
  }, [stableIdent]);

  const closeStream = () => {
    streamRef.current?.close();
    streamRef.current = null;
  };

  const finishLive = useCallback(
    async (id: string) => {
      closeStream();
      sessionRef.current = null;
      setLiveSession(null, null);
      setLiveBusy("stopping");
      try {
        const final = await stopLiveSession(id, identRef.current);
        setSession(final);
        if (final.error) setLiveError(final.error);
        if (final.revision_id) {
          setLiveNote(
            t("Saved {count} objects as revision {revision}")
              .replace("{count}", String(final.annotation_count ?? 0))
              .replace("{revision}", final.revision_id.slice(0, 12)),
          );
          window.dispatchEvent(new Event("levi:sam3-updated"));
        }
      } catch (error) {
        setLiveError(message(error));
      } finally {
        setLiveBusy(null);
        setSession((current) =>
          current && current.id === id && current.state !== "failed"
            ? { ...current, state: "stopped" }
            : current,
        );
      }
    },
    [t],
  );

  const openStream = useCallback(
    (id: string) => {
      closeStream();
      const stream = new EventSource(liveEventsUrl(id, identRef.current, 0));
      streamRef.current = stream;
      const parse = (event: Event) => {
        try {
          return JSON.parse((event as MessageEvent).data);
        } catch {
          return null;
        }
      };
      stream.addEventListener("result", (event) => {
        const data = parse(event) as LiveResult | null;
        if (data) pushLiveResult(data);
      });
      stream.addEventListener("stats", (event) => {
        const data = parse(event) as LiveStats | null;
        if (data) setStats(data);
      });
      stream.addEventListener("error", (event) => {
        // A server `error` event carries data; a transport error does not
        // (EventSource then reconnects with Last-Event-ID by itself).
        const data =
          event instanceof MessageEvent && event.data
            ? (parse(event) as { message?: string } | null)
            : null;
        if (data?.message) setLiveError(data.message);
      });
      stream.addEventListener("closed", (event) => {
        const data = parse(event) as { error?: string | null } | null;
        if (data?.error) setLiveError(data.error);
        if (sessionRef.current === id) void finishLive(id);
        else closeStream();
      });
    },
    [finishLive],
  );

  const startLive = async () => {
    if (!model || liveBusy || sessionRef.current) return;
    setLiveBusy("starting");
    setLiveError(null);
    setLiveNote(null);
    setStats(null);
    try {
      const started = await startLiveSession(stableIdent, {
        episode_index: episodeId,
        model: model.name,
        save,
      });
      sessionRef.current = started.id;
      setSession(started);
      setLiveSession(
        started.id,
        episodeId,
        started.ready?.fps ?? status?.fps ?? null,
      );
      openStream(started.id);
    } catch (error) {
      setLiveError(message(error));
    } finally {
      setLiveBusy(null);
    }
  };

  const stopLive = () => {
    const id = sessionRef.current;
    if (id) void finishLive(id);
  };

  // Stop on episode change, unmount and page unload (keepalive request).
  useEffect(() => {
    const onUnload = () => {
      const id = sessionRef.current;
      if (id) stopLiveSessionOnUnload(id, identRef.current);
    };
    window.addEventListener("pagehide", onUnload);
    return () => {
      window.removeEventListener("pagehide", onUnload);
      onUnload();
      sessionRef.current = null;
      streamRef.current?.close();
      streamRef.current = null;
      setLiveSession(null, null);
    };
  }, [episodeId, stableIdent]);

  useEffect(() => {
    setSession(null);
    setStats(null);
    setLiveNote(null);
    setLiveError(null);
  }, [episodeId]);

  // Push the player clock: play/pause/seek/rate events plus a 1 s heartbeat. Fire-and-forget, at most one request in flight.
  const liveId =
    session && (session.state === "running" || session.state === "starting")
      ? session.id
      : null;
  useEffect(() => {
    if (!liveId) return;
    const send = createCoalescer(
      (clock: { playing: boolean; time: number; rate: number }) =>
        sendLiveClock(liveId, identRef.current, clock),
    );
    let bound: { el: HTMLVideoElement; segmentStart: number } | null = null;
    const push = () => {
      if (!bound) return;
      const { el, segmentStart } = bound;
      send({
        playing: !el.paused && !el.ended,
        time: Math.max(0, el.currentTime - segmentStart),
        rate: el.playbackRate > 0 ? Math.min(16, el.playbackRate) : 1,
      });
    };
    const events = [
      "play",
      "playing",
      "pause",
      "seeking",
      "seeked",
      "ratechange",
    ];
    const bind = () => {
      const next = primaryLiveVideo();
      if (next?.el === bound?.el) return;
      for (const name of events) bound?.el.removeEventListener(name, push);
      bound = next;
      for (const name of events) bound?.el.addEventListener(name, push);
      push();
    };
    const unsubscribe = subscribeLive(bind);
    bind();
    // Also while paused: a paused viewer is still watching, and the worker
    // stops a session that hears no clock for LEVI_SEG_LIVE_IDLE_SECONDS.
    const heartbeat = window.setInterval(() => {
      if (bound) push();
    }, 1000);
    return () => {
      unsubscribe();
      window.clearInterval(heartbeat);
      for (const name of events) bound?.el.removeEventListener(name, push);
    };
  }, [liveId]);

  // ------------------------------------------------------------ label
  const [engine, setEngine] = useState<"student" | "teacher">("student");
  const [scope, setScope] = useState<"episode" | "all" | "range">("episode");
  const [rangeText, setRangeText] = useState("");
  const [teacherPrompts, setTeacherPrompts] = useState("");
  const [labelJob, setLabelJob] = useState<JobView | null>(null);
  const [labelBusy, setLabelBusy] = useState(false);
  const [labelError, setLabelError] = useState<string | null>(null);

  const scopeEpisodes = (): number[] | null => {
    if (scope === "episode") return [episodeId];
    if (scope === "all") return null;
    const episodes = parseEpisodeList(rangeText);
    if (!episodes.length) throw new Error(t("Enter at least one episode"));
    return episodes;
  };

  const startLabel = async () => {
    setLabelBusy(true);
    setLabelError(null);
    try {
      const episodes = scopeEpisodes();
      if (engine === "student") {
        if (!model) throw new Error(t("Choose a student model"));
        const job = await startSegLabel(stableIdent, {
          model: model.name,
          episodes,
        });
        setLabelJob(fromSegJob(job));
      } else {
        const prompts = teacherPrompts.trim()
          ? splitConcepts(teacherPrompts)
          : (model?.concepts ?? []);
        if (!prompts.length) throw new Error(t("Enter the concepts to label"));
        const all = episodes ?? [...new Set(allEpisodes ?? [episodeId])];
        if (!all.length) throw new Error(t("Enter at least one episode"));
        const plan = {
          episode_indices: all,
          camera_keys: cameraKeys,
          prompts,
          start_frame: 0,
          max_frames: null,
          review_threshold: 0.6,
          accept_threshold: 0.9,
          provider: "sam3" as const,
        };
        const staged = await planSam3(stableIdent, plan);
        const run = await runSam3(stableIdent, {
          ...plan,
          plan_id: staged.plan_id,
        });
        if (!run.job_id) throw new Error(t("The teacher did not start a job"));
        setLabelJob({ id: run.job_id, engine: "teacher", status: "queued" });
      }
    } catch (error) {
      setLabelError(message(error));
    } finally {
      setLabelBusy(false);
    }
  };

  useJobPoll(
    labelJob,
    async (job) =>
      job.engine === "teacher"
        ? fromSam3Job(job.id, await fetchSam3Job(job.id, stableIdent))
        : fromSegJob(await fetchSegJob(job.id, stableIdent)),
    (next) => {
      setLabelJob((current) =>
        current?.id === next.id ? { ...current, ...next } : current,
      );
      if (next.status === "succeeded")
        window.dispatchEvent(new Event("levi:sam3-updated"));
    },
  );

  const cancelLabel = async () => {
    if (!labelJob) return;
    try {
      if (labelJob.engine === "teacher")
        await cancelSam3Job(labelJob.id, stableIdent);
      else
        setLabelJob(fromSegJob(await cancelSegJob(labelJob.id, stableIdent)));
    } catch (error) {
      setLabelError(message(error));
    }
  };

  // ------------------------------------------------------------ distil
  const [distilName, setDistilName] = useState("");
  const [distilConcepts, setDistilConcepts] = useState("");
  const [trainText, setTrainText] = useState("");
  const [validText, setValidText] = useState("");
  const [testText, setTestText] = useState("");
  const [epochs, setEpochs] = useState(20);
  const [architecture, setArchitecture] = useState("rf-detr-seg-small");
  const [distilJob, setDistilJob] = useState<JobView | null>(null);
  const [distilBusy, setDistilBusy] = useState(false);
  const [distilError, setDistilError] = useState<string | null>(null);

  const startDistil = async () => {
    setDistilBusy(true);
    setDistilError(null);
    try {
      const concepts = splitConcepts(distilConcepts || DEFAULT_CONCEPTS);
      const job = await startSegDistil(stableIdent, {
        name: distilName.trim(),
        concepts,
        sources: [
          {
            train: parseEpisodeList(trainText),
            valid: parseEpisodeList(validText),
            test: parseEpisodeList(testText),
          },
        ],
        architecture,
        epochs,
      });
      setDistilJob(fromSegJob(job));
    } catch (error) {
      setDistilError(message(error));
    } finally {
      setDistilBusy(false);
    }
  };

  useJobPoll(
    distilJob,
    async (job) => fromSegJob(await fetchSegJob(job.id, stableIdent)),
    (next) => {
      setDistilJob((current) =>
        current?.id === next.id ? { ...current, ...next } : current,
      );
      if (next.status === "succeeded")
        void refreshStatus().then(() => {
          if (next.model) setModelName(next.model);
        });
    },
  );

  const cancelDistil = async () => {
    if (!distilJob) return;
    try {
      setDistilJob(fromSegJob(await cancelSegJob(distilJob.id, stableIdent)));
    } catch (error) {
      setDistilError(message(error));
    }
  };

  const removeModel = async () => {
    if (!model) return;
    if (
      !window.confirm(t("Delete model {name}?").replace("{name}", model.name))
    )
      return;
    try {
      await deleteSegModel(model.name);
      await refreshStatus();
    } catch (error) {
      setStatusError(message(error));
    }
  };

  // Status on mount and every 15 s; resume a job started before a reload.
  useEffect(() => {
    resumed.current = false;
    let cancelled = false;
    const load = async () => {
      const next = await refreshStatus();
      if (cancelled || !next || resumed.current) return;
      resumed.current = true;
      const running = (kind: SegJob["kind"]) =>
        [...next.jobs]
          .reverse()
          .find((job) => job.kind === kind && ACTIVE.has(job.status));
      const label = running("label");
      const distil = running("distil");
      if (label) setLabelJob((current) => current ?? fromSegJob(label));
      if (distil) setDistilJob((current) => current ?? fromSegJob(distil));
    };
    void load();
    const timer = window.setInterval(() => void refreshStatus(), 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [refreshStatus]);

  const labelActive = !!labelJob && ACTIVE.has(labelJob.status);
  const distilActive = !!distilJob && ACTIVE.has(distilJob.status);
  const liveOn = !!session && ["starting", "running"].includes(session.state);
  const workerReady = !!status?.worker.ready;
  const teacherReady = !!status?.teacher.ready;
  const distilledModel = distilJob?.model
    ? status?.models.find((m) => m.name === distilJob.model)
    : null;
  const cameraStats = Object.entries(stats?.cameras ?? {});

  return (
    <T>
      {
        <section
          className="object-annotation-panel panel-raised fast-seg-panel"
          data-testid="fast-seg-panel"
        >
          <div className="object-annotation-head">
            <div>
              <p className="section-kicker">
                <T>Fast segmentation</T>
              </p>
              <h2>
                <T>Student model · live overlay and dataset labelling</T>
              </h2>
            </div>
            <span className="object-annotation-badge">
              <FaBolt size={10} /> <T>student model</T>
            </span>
          </div>

          {/* a. Status */}
          <div className="object-annotation-runtime fast-seg-status">
            <span className={workerReady ? "ready" : "muted"}>
              <T>Student worker</T>
              {": "}
              {workerReady ? <T>ready</T> : <T>not ready</T>}
            </span>
            <span className={teacherReady ? "ready" : "muted"}>
              <T>Teacher model</T>
              {": "}
              {teacherReady ? <T>ready</T> : <T>not ready</T>}
            </span>
            <span className={status?.gpu_lock ? "ready" : "muted"}>
              <T>GPU lock</T>
              {": "}
              {status?.gpu_lock ? <T>configured</T> : <T>not configured</T>}
            </span>
          </div>
          {status && !workerReady && status.worker.reason && (
            <p className="fast-seg-hint">
              {status.worker.reason}
              {" · "}
              <T>Install it with</T>{" "}
              <code>integrations/segmentation/setup.sh</code>
            </p>
          )}
          {status && !teacherReady && status.teacher.reason && (
            <p className="fast-seg-hint">{status.teacher.reason}</p>
          )}
          {statusError && <p className="fast-seg-error">{statusError}</p>}

          {/* b. Model */}
          <div className="object-annotation-controls">
            <label className="object-annotation-prompt">
              <span>
                <T>Student model</T>
              </span>
              <select
                value={modelName}
                onChange={(event) => setModelName(event.target.value)}
                disabled={!status?.models.length || liveOn}
              >
                {!status?.models.length && (
                  <option value="">{t("No models yet")}</option>
                )}
                {status?.models.map((m) => (
                  <option key={m.name} value={m.name}>
                    {m.name}
                    {m.for_this_dataset ? "" : " · " + t("other dataset")}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="object-annotation-run"
              onClick={() => void removeModel()}
              disabled={!model || liveOn || labelActive}
              title={t("Delete model")}
              aria-label={t("Delete model")}
            >
              <FaTrash size={11} />
            </button>
          </div>
          {model && <ModelCard model={model} />}

          {/* c. Live overlay */}
          <div className="object-annotation-step">
            <div className="object-annotation-step-head">
              <span className="object-annotation-step-number">01</span>
              <div>
                <strong>
                  <T>Live overlay</T>
                </strong>
                <span>
                  <T>
                    Segments this episode while it plays. Masks that arrive late
                    are dropped, never shown out of step.
                  </T>
                </span>
              </div>
            </div>
            <div className="object-annotation-controls">
              <button
                type="button"
                className={"object-annotation-run" + (liveOn ? "" : " sam3")}
                onClick={() => (liveOn ? stopLive() : void startLive())}
                disabled={!!liveBusy || (!liveOn && (!model || !workerReady))}
                aria-pressed={liveOn}
              >
                {liveBusy === "starting" ? (
                  <T>Loading model…</T>
                ) : liveBusy === "stopping" ? (
                  <T>Stopping…</T>
                ) : liveOn ? (
                  <>
                    <FaStop size={10} /> <T>Stop live overlay</T>
                  </>
                ) : (
                  <T>Start live overlay</T>
                )}
              </button>
              <label className="object-annotation-checkbox">
                <input
                  type="checkbox"
                  checked={save}
                  onChange={(event) => setSave(event.target.checked)}
                  disabled={liveOn || !!liveBusy}
                />
                <T>Save results when stopping</T>
              </label>
            </div>
            {liveError && <p className="fast-seg-error">{liveError}</p>}
            {liveNote && (
              <p className="object-annotation-runtime">
                <span className="ready">{liveNote}</span>
              </p>
            )}
            {liveOn && (
              <div className="fast-seg-stats">
                <table>
                  <thead>
                    <tr>
                      <th>
                        <T>Camera</T>
                      </th>
                      <th>
                        <T>frames/s</T>
                      </th>
                      <th>
                        <T>latency p50 / p95 (ms)</T>
                      </th>
                      <th>
                        <T>dropped / skipped</T>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {cameraStats.length === 0 && (
                      <tr>
                        <td colSpan={4}>
                          <T>Waiting for the first frames…</T>
                        </td>
                      </tr>
                    )}
                    {cameraStats.map(([camera, row]) => (
                      <tr key={camera}>
                        <td>
                          <code>{camera}</code>
                        </td>
                        <td>{num(row.fps)}</td>
                        <td>
                          {num(row.latency_ms_p50, 0)} /{" "}
                          {num(row.latency_ms_p95, 0)}
                        </td>
                        <td>
                          {row.dropped ?? 0} / {row.skipped ?? 0}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="fast-seg-hint">
                  <T>Inference p50</T> {num(stats?.infer_ms_p50, 0)} ms ·{" "}
                  <T>GPU memory</T> {num(stats?.vram_process_mib, 0)} /{" "}
                  {num(stats?.vram_peak_mib, 0)} MiB <T>(now / peak)</T>
                </p>
              </div>
            )}
          </div>

          {/* d. Label dataset */}
          <div className="object-annotation-step">
            <div className="object-annotation-step-head">
              <span className="object-annotation-step-number">02</span>
              <div>
                <strong>
                  <T>Label dataset</T>
                </strong>
                <span>
                  <T>
                    Writes object annotations for every camera of the chosen
                    episodes; other episodes keep their objects.
                  </T>
                </span>
              </div>
            </div>
            <div className="object-annotation-controls">
              <label>
                <span>
                  <T>Engine</T>
                </span>
                <select
                  value={engine}
                  onChange={(event) =>
                    setEngine(event.target.value as "student" | "teacher")
                  }
                  disabled={labelActive || labelBusy}
                >
                  <option value="student">{t("Fast (student model)")}</option>
                  <option value="teacher">
                    {t("Quality (teacher model)")}
                  </option>
                </select>
              </label>
              <label>
                <span>
                  <T>Episodes</T>
                </span>
                <select
                  value={scope}
                  onChange={(event) =>
                    setScope(event.target.value as "episode" | "all" | "range")
                  }
                  disabled={labelActive || labelBusy}
                >
                  <option value="episode">{t("This episode")}</option>
                  <option value="all">{t("All episodes")}</option>
                  <option value="range">{t("Episode list")}</option>
                </select>
              </label>
              {scope === "range" && (
                <label>
                  <span>
                    <T>Episode list</T>
                  </span>
                  <input
                    value={rangeText}
                    onChange={(event) => setRangeText(event.target.value)}
                    placeholder="0-20, 25"
                    disabled={labelActive || labelBusy}
                  />
                </label>
              )}
              {engine === "teacher" && (
                <label className="object-annotation-prompt">
                  <span>
                    <T>Concepts (default: the model&apos;s concepts)</T>
                  </span>
                  <input
                    value={teacherPrompts}
                    onChange={(event) => setTeacherPrompts(event.target.value)}
                    placeholder={model?.concepts.join(", ") || DEFAULT_CONCEPTS}
                    disabled={labelActive || labelBusy}
                  />
                </label>
              )}
              <button
                type="button"
                className="object-annotation-run sam3"
                onClick={() => void startLabel()}
                disabled={
                  labelActive ||
                  labelBusy ||
                  (engine === "student"
                    ? !model || !workerReady
                    : !teacherReady)
                }
              >
                <T>Start labelling</T>
              </button>
            </div>
            {labelError && <p className="fast-seg-error">{labelError}</p>}
            {labelJob && (
              <JobProgress job={labelJob} onCancel={() => void cancelLabel()} />
            )}
          </div>

          {/* e. Distil */}
          <details
            className="object-annotation-step fast-seg-advanced"
            open={distilJob && ACTIVE.has(distilJob.status) ? true : undefined}
          >
            <summary>
              <span className="object-annotation-step-number">03</span>
              <strong>
                <T>Distil a student model (advanced)</T>
              </strong>
            </summary>
            <p className="fast-seg-hint">
              <T>
                The teacher model labels the training episodes, a student model
                learns from them, and the held-out episodes score the student
                against the teacher.
              </T>
            </p>
            <div className="object-annotation-controls">
              <label>
                <span>
                  <T>Model name</T>
                </span>
                <input
                  value={distilName}
                  onChange={(event) => setDistilName(event.target.value)}
                  placeholder="plates-student-v1"
                  disabled={distilActive || distilBusy}
                />
              </label>
              <label className="object-annotation-prompt">
                <span>
                  <T>Concepts (comma separated)</T>
                </span>
                <input
                  value={distilConcepts}
                  onChange={(event) => setDistilConcepts(event.target.value)}
                  placeholder={DEFAULT_CONCEPTS}
                  disabled={distilActive || distilBusy}
                />
              </label>
            </div>
            <div className="object-annotation-controls">
              <label>
                <span>
                  <T>Training episodes</T>
                </span>
                <input
                  value={trainText}
                  onChange={(event) => setTrainText(event.target.value)}
                  placeholder="0-20, 25"
                  disabled={distilActive || distilBusy}
                />
              </label>
              <label>
                <span>
                  <T>Validation episodes</T>
                </span>
                <input
                  value={validText}
                  onChange={(event) => setValidText(event.target.value)}
                  placeholder="21-23"
                  disabled={distilActive || distilBusy}
                />
              </label>
              <label>
                <span>
                  <T>Held-out episodes</T>
                </span>
                <input
                  value={testText}
                  onChange={(event) => setTestText(event.target.value)}
                  placeholder="24, 26"
                  disabled={distilActive || distilBusy}
                />
              </label>
              <label>
                <span>
                  <T>Epochs</T>
                </span>
                <input
                  type="number"
                  min={1}
                  max={200}
                  value={epochs}
                  onChange={(event) =>
                    setEpochs(
                      Math.max(1, Math.min(200, Number(event.target.value))),
                    )
                  }
                  disabled={distilActive || distilBusy}
                />
              </label>
              <label>
                <span>
                  <T>Architecture</T>
                </span>
                <select
                  value={architecture}
                  onChange={(event) => setArchitecture(event.target.value)}
                  disabled={distilActive || distilBusy}
                >
                  {ARCHITECTURES.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="object-annotation-run sam3"
                onClick={() => void startDistil()}
                disabled={
                  distilActive ||
                  distilBusy ||
                  !distilName.trim() ||
                  !trainText.trim() ||
                  !workerReady ||
                  !teacherReady
                }
              >
                <T>Start distillation</T>
              </button>
            </div>
            {distilError && <p className="fast-seg-error">{distilError}</p>}
            {distilJob && (
              <JobProgress
                job={distilJob}
                onCancel={() => void cancelDistil()}
              />
            )}
            {distilJob?.status === "succeeded" && distilledModel && (
              <ModelCard model={distilledModel} />
            )}
          </details>
        </section>
      }
    </T>
  );
}
