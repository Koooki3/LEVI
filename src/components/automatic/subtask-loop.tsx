"use client";
// The subtask-loop view of a run (reset mode `subtask_loop`): the round and
// loop the run is on, each loop's plan with every subtask's instruction,
// status, attempts and the answer provider's done check with its frames, the
// frozen object whitelist and whether the model is asleep or awake. It only
// shows: there is no control here. The operator confirms the objects and
// releases each round's forward loop at the run's terminal (decision UI-2),
// and the model's "done" is a judgement, never a label.
import { Fragment } from "react";
import { Badge, type Tone } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Frames } from "./pending-card";
import {
  SUBTASK_STATUS_KEY,
  VLM_STATE_KEY,
  foldSubtasks,
  subtaskReason,
  type FrameRef,
  type CheckValue,
  type LoopView,
  type SubtaskLoopView,
  type SubtaskRow,
  type SubtaskStatus,
  type VlmView,
} from "./subtask-logic";
import type {
  RequestEvidence,
  SubtaskProgress,
  SubtaskProvider,
  SubtasksResponse,
} from "./types";

type T = (key: string) => string;

const STATUS_TONE: Record<SubtaskStatus, Tone> = {
  pending: "neutral",
  running: "info",
  done: "success",
  retrying: "warning",
  failed: "danger",
  unknown: "warning",
  hidden: "neutral",
};

function checkText(value: CheckValue, t: T): string {
  if (value === undefined) return t("automatic.subtask.check.pending");
  if (value === "hidden") return t("automatic.subtask.check.hidden");
  if (value === null) return t("automatic.subtask.check.null");
  return t(
    value ? "automatic.subtask.check.true" : "automatic.subtask.check.false",
  );
}

const INVENTORY_KEY = {
  proposed: "automatic.subtask.inventory.proposed",
  confirmed: "automatic.subtask.inventory.confirmed",
  rejected: "automatic.subtask.inventory.rejected",
} as const;

const loopKey = (loop: string) =>
  loop === "reverse"
    ? "automatic.subtask.loop.reverse"
    : "automatic.subtask.loop.forward";

export function SubtaskLoopView({
  runId,
  data,
  error = "",
  hiddenEpisodes,
  maxRetries = null,
  progress = null,
  provider = null,
}: {
  runId: string;
  data: SubtasksResponse | null;
  error?: string;
  hiddenEpisodes?: Set<string>;
  maxRetries?: number | null;
  /** The snapshot's `subtask` (where the loop is), when the server sends it. */
  progress?: SubtaskProgress | null;
  provider?: SubtaskProvider | null;
}) {
  const { t } = useLocale();
  const providerMode =
    provider?.source === "frozen_plan" &&
    (provider.mode === "local" || provider.mode === "external")
      ? provider.mode
      : "unknown";
  const view = foldSubtasks(data, { hiddenEpisodes, maxRetries });
  const empty = !view.inventory && view.rounds.length === 0 && !view.vlm;
  return (
    <section
      className="ar-section"
      aria-labelledby="ar-subtask-title"
      data-testid="subtask-loop"
    >
      <h2 id="ar-subtask-title">{t("automatic.subtask.title")}</h2>
      <p className="ar-muted">{t("automatic.subtask.intro")}</p>
      <dl className="ar-dl" data-testid="subtask-provider">
        <dt>{t("automatic.subtask.provider.title")}</dt>
        <dd>{t(`automatic.subtask.provider.${providerMode}`)}</dd>
      </dl>
      <p className="ar-muted">{t("automatic.subtask.provider.note")}</p>
      <p className="ar-note ar-note--info" role="note">
        {t("automatic.subtask.model_note")}
      </p>
      {error && (
        <p className="ar-muted" role="status">
          {t("automatic.subtask.failed_load")} ({error})
        </p>
      )}
      {view.truncated && (
        <p className="ar-muted">{t("automatic.subtask.truncated")}</p>
      )}
      {view.unreadable > 0 && (
        <p className="ar-muted" data-testid="subtask-unreadable">
          {t("automatic.subtask.unreadable").replace(
            "{n}",
            String(view.unreadable),
          )}
        </p>
      )}
      {data?.request_evidence_truncated && (
        <p>{t("automatic.subtask.evidence.limited")}</p>
      )}
      <RequestEvidenceList rows={data?.request_evidence ?? []} t={t} />
      {empty && !error ? (
        <p className="ar-muted">{t("automatic.subtask.waiting_notes")}</p>
      ) : (
        <>
          <NowList view={view} progress={progress} t={t} />
          {view.serviceSteps.length > 0 && (
            <details className="ar-raw" data-testid="subtask-services">
              <summary>
                {t("automatic.subtask.services").replace(
                  "{n}",
                  String(view.serviceSteps.length),
                )}
              </summary>
              <ol className="ar-subtask-attempts">
                {view.serviceSteps.map((s, i) => (
                  <li key={`${s.step}-${s.key}-${i}`}>
                    <span>{s.step ?? "?"}</span>
                    <code>{s.key}</code>
                    <span>
                      {s.ok === null
                        ? "—"
                        : t(
                            s.ok
                              ? "automatic.subtask.services.ok"
                              : "automatic.subtask.services.failed",
                          )}
                    </span>
                  </li>
                ))}
              </ol>
            </details>
          )}
          <Inventory runId={runId} view={view} t={t} />
          {view.rounds
            .slice()
            .reverse()
            .map((round) => (
              <div
                key={round.round}
                className="ar-subtask-round"
                data-round={round.round}
              >
                <h3>
                  {t("automatic.subtask.round_n").replace(
                    "{n}",
                    String(round.round),
                  )}
                </h3>
                {round.released && (
                  <p className="ar-muted" data-testid="subtask-released">
                    {t("automatic.subtask.round.released").replace(
                      "{by}",
                      subtaskReason(round.released, t),
                    )}
                  </p>
                )}
                {round.abandoned && (
                  <p className="ar-note ar-note--warning" role="note">
                    {t("automatic.subtask.round.abandoned")}{" "}
                    {subtaskReason(round.abandoned, t)}
                  </p>
                )}
                {round.loops.map((loop) => (
                  <Loop
                    key={loop.loop}
                    runId={runId}
                    loop={loop}
                    maxRetries={maxRetries}
                    t={t}
                  />
                ))}
              </div>
            ))}
        </>
      )}
    </section>
  );
}

const PHASE_KEY: Record<string, string> = {
  idle: "automatic.subtask.phase.idle",
  forward: "automatic.subtask.phase.forward",
  reverse_pending: "automatic.subtask.phase.reverse_pending",
  reverse: "automatic.subtask.phase.reverse",
};

function NowList({
  view,
  progress,
  t,
}: {
  view: SubtaskLoopView;
  progress: SubtaskProgress | null;
  t: T;
}) {
  const now = view.current;
  // The snapshot's phase (the server's reading of the same notes) when
  // there is one; otherwise the loop of the latest note.
  const phase = progress?.phase ? PHASE_KEY[progress.phase] : undefined;
  return (
    <dl className="ar-dl" data-testid="subtask-now">
      <dt>{t("automatic.subtask.round")}</dt>
      <dd data-row="round">{progress?.round ?? now?.round ?? "—"}</dd>
      {typeof progress?.rounds_ended === "number" && (
        <>
          <dt>{t("automatic.subtask.rounds_ended")}</dt>
          <dd data-row="rounds_ended">{progress.rounds_ended}</dd>
        </>
      )}
      <dt>{t("automatic.subtask.phase")}</dt>
      <dd data-row="loop">
        {phase
          ? t(phase)
          : progress?.phase
            ? progress.phase
            : now
              ? t(loopKey(now.loop))
              : "—"}
      </dd>
      <dt>{t("automatic.subtask.current")}</dt>
      <dd data-row="subtask">
        {now && now.index !== null
          ? t("automatic.subtask.subtask_n").replace(
              "{n}",
              String(now.index + 1),
            )
          : t("automatic.subtask.none_yet")}
      </dd>
      <dt>{t("automatic.subtask.vlm.title")}</dt>
      <dd data-row="vlm">
        <VlmText vlm={view.vlm} t={t} />
      </dd>
      {view.alternation && (
        <>
          <dt>{t("automatic.subtask.alternation")}</dt>
          <dd data-row="alternation">
            {t(
              view.alternation.mode === "co_resident"
                ? "automatic.subtask.alternation.co_resident"
                : "automatic.subtask.alternation.sleep",
            )}
            {!view.alternation.ok &&
              ` · ${t("automatic.subtask.alternation.refused")}`}
          </dd>
        </>
      )}
      <dt>{t("automatic.subtask.vlm.mem")}</dt>
      <dd data-row="mem">
        {view.vlm?.memAvailableGib != null
          ? t("automatic.subtask.vlm.mem_value").replace(
              "{gib}",
              view.vlm.memAvailableGib.toFixed(1),
            )
          : "—"}
      </dd>
    </dl>
  );
}

function VlmText({ vlm, t }: { vlm: VlmView | null; t: T }) {
  if (!vlm) return <>{t("automatic.subtask.vlm.unknown")}</>;
  const tone: Tone =
    vlm.state === "wake_failed" ||
    vlm.state === "wake_refused" ||
    vlm.state === "sleep_refused"
      ? "danger"
      : vlm.state === "awake" || vlm.state === "idle"
        ? "info"
        : "neutral";
  return (
    <span className="ar-chips">
      <Badge tone={tone}>{t(VLM_STATE_KEY[vlm.state])}</Badge>
      {vlm.ms != null && (
        <span className="ar-muted">
          {t("automatic.subtask.vlm.took").replace("{ms}", String(vlm.ms))}
        </span>
      )}
      {vlm.reason === "not_ok" ? (
        <span>{t("automatic.subtask.vlm.not_ok")}</span>
      ) : (
        vlm.reason && <span>{subtaskReason(vlm.reason, t)}</span>
      )}
    </span>
  );
}

function Inventory({
  runId,
  view,
  t,
}: {
  runId: string;
  view: SubtaskLoopView;
  t: T;
}) {
  const inv = view.inventory;
  if (!inv) return null;
  const tone: Tone =
    inv.status === "confirmed"
      ? "success"
      : inv.status === "rejected"
        ? "danger"
        : "warning";
  return (
    <div className="ar-subtask-inventory" data-testid="subtask-inventory">
      <h3>{t("automatic.subtask.inventory.title")}</h3>
      <p className="ar-chips">
        <Badge tone={tone}>{t(INVENTORY_KEY[inv.status])}</Badge>
        {inv.by && (
          <span className="ar-muted">
            {t("automatic.subtask.inventory.by").replace("{by}", inv.by)}
          </span>
        )}
      </p>
      {inv.items.length > 0 && (
        <ul className="ar-subtask-items">
          {inv.items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      )}
      {inv.frames.length > 0 && (
        <details className="ar-raw">
          <summary>{t("automatic.subtask.inventory.frames")}</summary>
          <FrameRefs runId={runId} frames={inv.frames} t={t} />
        </details>
      )}
    </div>
  );
}

function Loop({
  runId,
  loop,
  maxRetries,
  t,
}: {
  runId: string;
  loop: LoopView;
  maxRetries: number | null;
  t: T;
}) {
  return (
    <div className="ar-subtask-loop" data-loop={loop.loop}>
      <h4>{t(loopKey(loop.loop))}</h4>
      {loop.task && (
        <p>
          <span className="ar-muted">{t("automatic.subtask.plan.task")}: </span>
          {loop.task}
        </p>
      )}
      {loop.refused && (
        <p className="ar-note ar-note--warning" role="note">
          <strong>{t("automatic.subtask.plan.refused")}</strong>{" "}
          {subtaskReason(loop.refused, t)}
        </p>
      )}
      {loop.planned !== null && (
        <p className="ar-muted">
          {t("automatic.subtask.plan.count").replace(
            "{n}",
            String(loop.planned),
          )}
        </p>
      )}
      {loop.planSha256 && !loop.planOk && (
        <p className="ar-muted" data-testid="subtask-plan-problem">
          {t("automatic.subtask.plan.problem").replace(
            "{problem}",
            loop.planProblem ?? "",
          )}
        </p>
      )}
      {loop.subtasks.length === 0 && !loop.refused ? (
        <p className="ar-muted">{t("automatic.subtask.plan.empty")}</p>
      ) : (
        <ol className="ar-subtask-list">
          {loop.subtasks.map((row) => (
            <Subtask
              key={row.index}
              runId={runId}
              row={row}
              reverse={loop.loop === "reverse"}
              maxRetries={maxRetries}
              t={t}
            />
          ))}
        </ol>
      )}
      {loop.done && (
        <p className="ar-muted" data-testid="subtask-loop-done">
          {t("automatic.subtask.plan.done")
            .replace("{n}", String(loop.done.subtasks ?? "—"))
            .replace("{r}", String(loop.done.retries ?? "—"))}
        </p>
      )}
    </div>
  );
}

function Subtask({
  runId,
  row,
  reverse,
  maxRetries,
  t,
}: {
  runId: string;
  row: SubtaskRow;
  reverse: boolean;
  maxRetries: number | null;
  t: T;
}) {
  const last = row.attempts[row.attempts.length - 1];
  return (
    <li
      className="ar-subtask"
      data-index={row.index}
      data-status={row.status}
      aria-current={row.status === "running" ? "step" : undefined}
    >
      <div className="ar-subtask__head">
        <strong>
          {t("automatic.subtask.subtask_n").replace(
            "{n}",
            String(row.index + 1),
          )}
        </strong>
        <Badge tone={STATUS_TONE[row.status]}>
          {t(SUBTASK_STATUS_KEY[row.status])}
        </Badge>
        {row.attempts.length > 0 && (
          <span className="ar-muted" data-testid="subtask-attempts">
            {(maxRetries != null
              ? t("automatic.subtask.attempts_of").replace(
                  "{max}",
                  String(maxRetries),
                )
              : t("automatic.subtask.attempts")
            ).replace("{n}", String(row.attempts.length))}
          </span>
        )}
      </div>
      <dl className="ar-dl">
        {row.instruction && (
          <>
            <dt>{t("automatic.subtask.instruction")}</dt>
            <dd>{row.instruction}</dd>
          </>
        )}
        {row.pick && row.place && (
          <>
            <dt>{t("automatic.subtask.objects")}</dt>
            <dd>
              {t("automatic.subtask.move")
                .replace("{pick}", row.pick)
                .replace("{place}", row.place)}
            </dd>
          </>
        )}
        {row.doneWhen && (
          <>
            <dt>{t("automatic.subtask.done_when")}</dt>
            <dd>{row.doneWhen}</dd>
          </>
        )}
        {last && (
          <>
            <dt>{t("automatic.subtask.check")}</dt>
            <dd data-testid="subtask-check">{checkText(last.check, t)}</dd>
          </>
        )}
      </dl>
      {row.attempts.length > 0 && (
        <details className="ar-raw">
          <summary>
            {t("automatic.subtask.attempt_list").replace(
              "{n}",
              String(row.attempts.length),
            )}
          </summary>
          <ol className="ar-subtask-attempts">
            {row.attempts.map((a, i) => (
              <li key={`${a.attempt}-${i}`}>
                <span>
                  {t("automatic.subtask.attempt_n").replace(
                    "{n}",
                    String(i + 1),
                  )}
                </span>
                {a.episodeId && (
                  <span>
                    {t("automatic.subtask.episode")}: <code>{a.episodeId}</code>
                    {reverse && (
                      <span className="ar-muted">
                        {" "}
                        ({t("automatic.subtask.reset_role")})
                      </span>
                    )}
                  </span>
                )}
                <span>{checkText(a.check, t)}</span>
                {a.frames.length > 0 && a.check !== "hidden" && (
                  <FrameRefs runId={runId} frames={a.frames} t={t} />
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
    </li>
  );
}

/** Stored frames as images (the run's evidence frames); a frame that is not
 * stored is named by its reference only. */
function FrameRefs({
  runId,
  frames,
  t,
}: {
  runId: string;
  frames: FrameRef[];
  t: T;
}) {
  const stored = frames.filter((f) => f.stored).map((f) => f.sha256);
  const missing = frames.filter((f) => !f.stored);
  return (
    <>
      {stored.length > 0 && <Frames runId={runId} shas={stored} />}
      {missing.length > 0 && (
        <p className="ar-muted" data-testid="subtask-frames-missing">
          {t("automatic.subtask.frames_not_stored")}{" "}
          {missing.map((f) => f.ref ?? f.sha256.slice(0, 12)).join(", ")}
        </p>
      )}
    </>
  );
}

function RequestEvidenceList({ rows, t }: { rows: RequestEvidence[]; t: T }) {
  if (rows.length === 0) return null;
  const unknown = t("automatic.subtask.evidence.unknown");
  return (
    <section data-testid="request-evidence">
      <h3>{t("automatic.subtask.evidence.title")}</h3>
      <p className="ar-muted">{t("automatic.subtask.evidence.note")}</p>
      {rows.map((row) => {
        const report =
          row.visibility === "visible" && row.context ? row.report : null;
        return (
          <details className="ar-raw" key={row.seq}>
            <summary>
              #{row.seq} ·{" "}
              {report
                ? row.context?.round_id
                : t("automatic.subtask.evidence.hidden")}
            </summary>
            {report ? (
              <>
                <p>
                  {t(`automatic.subtask.evidence.kind.${row.context?.kind}`)} ·{" "}
                  {row.context?.episode_id ?? unknown}
                </p>
                <dl className="ar-dl">
                  <dt>{t("automatic.subtask.evidence.agent_id")}</dt>
                  <dd>{report.agent_id ?? unknown}</dd>
                  {(
                    [
                      "input_new",
                      "output",
                      "cache_read",
                      "cache_write",
                    ] as const
                  ).map((key) => (
                    <Fragment key={key}>
                      <dt>{t(`automatic.subtask.evidence.${key}`)}</dt>
                      <dd>{report.tokens[key] ?? unknown}</dd>
                    </Fragment>
                  ))}
                  {(
                    [
                      "client_latency_ms",
                      "bridge_wait_ms",
                      "self_reported_elapsed_ms",
                    ] as const
                  ).map((key) => (
                    <Fragment key={key}>
                      <dt>{t(`automatic.subtask.evidence.${key}`)}</dt>
                      <dd>{report[key] ?? unknown}</dd>
                    </Fragment>
                  ))}
                </dl>
                <p>{t("automatic.subtask.evidence.agent_notes")}</p>
                <pre>{report.agent_notes ?? unknown}</pre>
                <p>{t("automatic.subtask.evidence.prompt")}</p>
                <pre>{report.prompt_text ?? unknown}</pre>
                <p>{t("automatic.subtask.evidence.answer")}</p>
                <pre>{report.raw_content ?? unknown}</pre>
                {(report.prompt_text_truncated ||
                  report.raw_content_truncated ||
                  report.display_text_truncated) && (
                  <p>{t("automatic.subtask.evidence.truncated")}</p>
                )}
                <p>{t("automatic.subtask.evidence.full_metadata")}</p>
                <dl className="ar-dl">
                  <dt>{t("automatic.subtask.evidence.prompt_bytes")}</dt>
                  <dd>{report.prompt_bytes ?? unknown}</dd>
                  <dt>{t("automatic.subtask.evidence.answer_bytes")}</dt>
                  <dd>{report.answer_bytes ?? unknown}</dd>
                  <dt>{t("automatic.subtask.evidence.prompt_sha256")}</dt>
                  <dd>
                    <code>{report.prompt_sha256 ?? unknown}</code>
                  </dd>
                  <dt>{t("automatic.subtask.evidence.answer_sha256")}</dt>
                  <dd>
                    <code>{report.answer_content_sha256 ?? unknown}</code>
                  </dd>
                </dl>
              </>
            ) : (
              <p>{t("automatic.subtask.evidence.hidden")}</p>
            )}
          </details>
        );
      })}
    </section>
  );
}
