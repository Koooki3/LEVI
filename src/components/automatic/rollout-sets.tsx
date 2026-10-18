"use client";
// The rollout sets of a subtask-loop run (GET runs/{id}/rollouts): each round
// keeps a forward set and a reverse set, each set its subtasks, each subtask
// every attempt (one recorded demo per policy segment, retries included).
// Read-only: nothing here writes. A check is the model's judgement from still
// frames, never a label; the operator's label is shown when there is one.
// Videos are played from LEVI's dataset file service with the viewer's video
// component, and only mounted when a person opens an attempt's videos.
import Link from "next/link";
import { useState } from "react";
import { Badge, type Tone } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { AnnotationsProvider } from "@/context/annotations-context";
import { TimeProvider } from "@/context/time-context";
import PlaybackBar from "@/components/playback-bar";
import { SimpleVideosPlayer } from "@/components/simple-videos-player";
import { useRunRollouts } from "./run-poll";
import type {
  Rollout,
  RolloutAttempt,
  RolloutSet,
  RolloutSubtask,
  RolloutsResponse,
} from "./types";

type T = (key: string) => string;

const HIDDEN = "hidden_until_labelled";
const SET_KINDS = ["forward", "reverse"] as const;

// Blind rule: a forward check, its stop reason and the outcomes derived from
// them arrive as this code until the round is labelled.
const OUTCOME_TONE: Record<string, Tone> = {
  forward_done: "success",
  forward_failed: "danger",
  abandoned: "warning",
  locked: "danger",
  incomplete: "neutral",
  discarded: "neutral",
};
// A set's outcome is how its loop ended (`subtask_loop_done`), not a label:
// only the round's abandon and lock get a status colour.
const SET_TONE: Record<string, Tone> = {
  abandoned: "warning",
  locked: "danger",
};

/** A code's words from the catalogue, or the code itself when there are none. */
function codeText(prefix: string, code: string, t: T): string {
  const key = `${prefix}.${code}`;
  const text = t(key);
  return text === key ? code : text;
}

function checkText(attempt: RolloutAttempt, hidden: boolean, t: T): string {
  if (hidden || attempt.check?.hidden_until_labelled)
    return t("automatic.rollouts.hidden");
  if (!attempt.check) return t("automatic.subtask.check.pending");
  const value = attempt.check.value;
  if (value === null || value === undefined)
    return t("automatic.rollouts.check.null");
  return t(
    value ? "automatic.subtask.check.true" : "automatic.subtask.check.false",
  );
}

function labelText(value: RolloutAttempt["operator_label"]): string | null {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const found = (value as Record<string, unknown>).value;
    if (typeof found === "string") return found;
    return JSON.stringify(value);
  }
  return null;
}

function problemText(value: string | Record<string, unknown>): string {
  if (typeof value === "string") return value;
  const parts = Object.entries(value)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : String(v)}`);
  return parts.join(" · ");
}

/** The section on the run window: closed until a person opens it, then the
 * manifest is fetched (and refreshed while the run goes on). */
export function RolloutSetsSection({
  runId,
  seq,
  hiddenEpisodes,
}: {
  runId: string;
  seq: number | undefined;
  hiddenEpisodes?: Set<string>;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const found = useRunRollouts(runId, seq, open);
  return (
    <section
      className="ar-section"
      aria-labelledby="ar-rollouts-title"
      data-testid="rollout-sets"
    >
      <h2 id="ar-rollouts-title">{t("automatic.rollouts.title")}</h2>
      <p className="ar-muted">{t("automatic.rollouts.intro")}</p>
      <details
        className="ar-raw"
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>{t("automatic.rollouts.open")}</summary>
        {open && (
          <RolloutSets
            data={found.data}
            error={found.error}
            loading={found.loading}
            hiddenEpisodes={hiddenEpisodes}
          />
        )}
      </details>
    </section>
  );
}

export function RolloutSets({
  data,
  error = "",
  loading = false,
  hiddenEpisodes,
}: {
  data: RolloutsResponse | null;
  error?: string;
  loading?: boolean;
  hiddenEpisodes?: Set<string>;
}) {
  const { t } = useLocale();
  const hidden = hiddenEpisodes ?? new Set<string>();
  return (
    <div className="ar-rollouts" data-testid="rollout-sets-body">
      <p className="ar-note ar-note--info" role="note">
        {t("automatic.rollouts.model_note")}
      </p>
      {error && (
        <p className="ar-muted" role="status">
          {t("automatic.rollouts.failed")} ({error})
        </p>
      )}
      {loading && !data && (
        <p className="ar-muted" role="status">
          {t("automatic.rollouts.loading")}
        </p>
      )}
      {data && (
        <>
          <dl className="ar-dl" data-testid="rollouts-summary">
            {data.task && (
              <>
                <dt>{t("automatic.rollouts.task")}</dt>
                <dd>{data.task}</dd>
              </>
            )}
            <dt>{t("automatic.rollouts.mode")}</dt>
            <dd>{t(`automatic.subtask.provider.${data.mode}`)}</dd>
            <dt>{t("automatic.rollouts.rounds")}</dt>
            <dd>{data.rollouts.length}</dd>
          </dl>
          {data.problems.length > 0 && (
            <details className="ar-raw" data-testid="rollouts-problems">
              <summary>
                {t("automatic.rollouts.problems").replace(
                  "{n}",
                  String(data.problems.length),
                )}
              </summary>
              <p className="ar-muted">
                {t("automatic.rollouts.problems_note")}
              </p>
              <ul className="ar-rollout-list">
                {data.problems.map((p, i) => (
                  <li key={i}>{problemText(p)}</li>
                ))}
              </ul>
            </details>
          )}
          {data.unmatched_demos.length > 0 && (
            <details className="ar-raw" data-testid="rollouts-unmatched">
              <summary>
                {t("automatic.rollouts.unmatched").replace(
                  "{n}",
                  String(data.unmatched_demos.length),
                )}
              </summary>
              <ul className="ar-rollout-list">
                {data.unmatched_demos.map((d, i) => (
                  <li key={i}>
                    <code>{problemText(d)}</code>
                  </li>
                ))}
              </ul>
            </details>
          )}
          {data.rollouts.length === 0 ? (
            <p className="ar-muted">{t("automatic.rollouts.empty")}</p>
          ) : (
            data.rollouts.map((rollout, i) => (
              <RoundView
                key={`${rollout.round_id ?? rollout.round}-${i}`}
                rollout={rollout}
                hidden={hidden}
                t={t}
              />
            ))
          )}
          {data.rollouts_truncated && (
            <p className="ar-muted">{t("automatic.rollouts.truncated")}</p>
          )}
        </>
      )}
    </div>
  );
}

function RoundView({
  rollout,
  hidden,
  t,
}: {
  rollout: Rollout;
  hidden: Set<string>;
  t: T;
}) {
  const id = `ar-rollout-round-${rollout.round ?? "x"}-${rollout.round_id ?? ""}`;
  return (
    <section
      className="ar-subtask-round"
      aria-labelledby={id}
      data-testid="rollout-round"
      data-round={rollout.round ?? ""}
    >
      <div className="ar-subtask__head">
        <h3 id={id}>
          {t("automatic.rollouts.round").replace(
            "{n}",
            String(rollout.round ?? "?"),
          )}
        </h3>
        {rollout.outcome && (
          <Badge tone={OUTCOME_TONE[rollout.outcome] ?? "neutral"}>
            {codeText("automatic.rollouts.outcome", rollout.outcome, t)}
          </Badge>
        )}
      </div>
      {rollout.released_at && (
        <p className="ar-muted">
          {t("automatic.rollouts.released_at")} {rollout.released_at}
        </p>
      )}
      {rollout.abandoned && (
        <p className="ar-muted" data-testid="rollout-abandoned">
          {t("automatic.rollouts.abandoned")}{" "}
          {[rollout.abandoned.phase, rollout.abandoned.why]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
      {rollout.locked && (
        <p className="ar-muted" data-testid="rollout-locked">
          {t("automatic.rollouts.locked")}{" "}
          {[rollout.locked.reason, rollout.locked.at]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
      {labelText(rollout.operator_label ?? null) && (
        <p data-testid="rollout-round-label">
          {t("automatic.rollouts.round_label")}{" "}
          {labelText(rollout.operator_label ?? null)}
        </p>
      )}
      {rollout.notes && rollout.notes.length > 0 && (
        <details className="ar-raw" data-testid="rollout-notes">
          <summary>
            {t("automatic.rollouts.notes").replace(
              "{n}",
              String(rollout.notes.length),
            )}
          </summary>
          <ul className="ar-rollout-list">
            {rollout.notes.map((note, i) => (
              <li key={i}>
                <code>{note.code}</code>
                {note.at && <span className="ar-muted"> {note.at}</span>}
                {note.detail && <> · {note.detail}</>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {SET_KINDS.map((kind) => (
        <SetView
          key={kind}
          kind={kind}
          value={rollout.sets[kind]}
          hidden={hidden}
          t={t}
        />
      ))}
    </section>
  );
}

function SetView({
  kind,
  value,
  hidden,
  t,
}: {
  kind: (typeof SET_KINDS)[number];
  value: RolloutSet | null;
  hidden: Set<string>;
  t: T;
}) {
  const title = t(`automatic.rollouts.set.${kind}`);
  return (
    <div className="ar-subtask-loop" data-testid={`rollout-set-${kind}`}>
      <div className="ar-subtask__head">
        <h4>{title}</h4>
        {value?.outcome && (
          <Badge tone={SET_TONE[value.outcome] ?? "neutral"}>
            {codeText("automatic.rollouts.set_outcome", value.outcome, t)}
          </Badge>
        )}
      </div>
      {value?.task && (
        <p className="ar-muted">
          {t("automatic.rollouts.task")}: {value.task}
        </p>
      )}
      {!value ? (
        <p className="ar-muted">{t("automatic.rollouts.set.none")}</p>
      ) : value.subtasks.length === 0 ? (
        <p className="ar-muted">{t("automatic.rollouts.set.no_subtasks")}</p>
      ) : (
        <ol className="ar-subtask-list">
          {value.subtasks.map((subtask, i) => (
            <SubtaskView
              key={`${subtask.index}-${i}`}
              subtask={subtask}
              hidden={hidden}
              t={t}
            />
          ))}
        </ol>
      )}
    </div>
  );
}

function SubtaskView({
  subtask,
  hidden,
  t,
}: {
  subtask: RolloutSubtask;
  hidden: Set<string>;
  t: T;
}) {
  return (
    <li
      className="ar-subtask"
      data-testid="rollout-subtask"
      data-index={subtask.index ?? ""}
    >
      <div className="ar-subtask__head">
        <strong>
          {t("automatic.rollouts.subtask").replace(
            "{n}",
            String((subtask.index ?? 0) + 1),
          )}
        </strong>
        <span className="ar-muted">
          {t("automatic.rollouts.attempts").replace(
            "{n}",
            String(subtask.attempts.length),
          )}
        </span>
      </div>
      {subtask.instruction && (
        <dl className="ar-dl">
          <dt>{t("automatic.subtask.instruction")}</dt>
          <dd>{subtask.instruction}</dd>
        </dl>
      )}
      <ol className="ar-rollout-attempts">
        {subtask.attempts.map((attempt, i) => (
          <AttemptView
            key={`${attempt.attempt}-${i}`}
            attempt={attempt}
            n={attempt.attempt ?? i + 1}
            instruction={subtask.instruction}
            hidden={hidden}
            t={t}
          />
        ))}
      </ol>
      {subtask.attempts_truncated && (
        <p className="ar-muted">{t("automatic.rollouts.truncated")}</p>
      )}
    </li>
  );
}

function AttemptView({
  attempt,
  n,
  instruction,
  hidden,
  t,
}: {
  attempt: RolloutAttempt;
  n: number;
  instruction: string | null;
  hidden: Set<string>;
  t: T;
}) {
  const withheld = !!attempt.episode_id && hidden.has(attempt.episode_id);
  const label = labelText(attempt.operator_label);
  const stop = withheld ? HIDDEN : attempt.stop_reason;
  return (
    <li className="ar-rollout-attempt" data-testid="rollout-attempt">
      <div className="ar-subtask__head">
        <strong>
          {t("automatic.subtask.attempt_n").replace("{n}", String(n))}
        </strong>
        {attempt.episode_id && <code>{attempt.episode_id}</code>}
      </div>
      <dl className="ar-dl">
        {attempt.instruction && attempt.instruction !== instruction && (
          <>
            <dt>{t("automatic.subtask.instruction")}</dt>
            <dd>{attempt.instruction}</dd>
          </>
        )}
        <dt>{t("automatic.rollouts.frames")}</dt>
        <dd data-row="frames">{attempt.frames ?? "—"}</dd>
        <dt>{t("automatic.rollouts.wall_time")}</dt>
        <dd data-row="seconds">
          {attempt.seconds == null
            ? "—"
            : t("automatic.rollouts.seconds_value").replace(
                "{s}",
                attempt.seconds.toFixed(1),
              )}
        </dd>
        <dt>{t("automatic.rollouts.stop_reason")}</dt>
        <dd data-row="stop">
          {stop ? codeText("automatic.rollouts.stop", stop, t) : "—"}
        </dd>
        <dt>{t("automatic.rollouts.check")}</dt>
        <dd data-row="check">{checkText(attempt, withheld, t)}</dd>
        <dt>{t("automatic.rollouts.operator_label")}</dt>
        <dd data-row="label">
          {label ?? t("automatic.rollouts.no_operator_label")}
        </dd>
        <dt>{t("automatic.rollouts.demo")}</dt>
        <dd data-row="demo">
          {attempt.demo ? (
            <code>{attempt.demo}</code>
          ) : attempt.incomplete ? (
            t("automatic.rollouts.incomplete")
          ) : (
            t("automatic.rollouts.no_demo")
          )}
        </dd>
      </dl>
      {attempt.incomplete?.why && (
        <p className="ar-muted" data-testid="rollout-incomplete">
          {attempt.incomplete.why}
        </p>
      )}
      {attempt.note && <p className="ar-muted">{attempt.note}</p>}
      <AttemptVideos attempt={attempt} t={t} />
    </li>
  );
}

function AttemptVideos({ attempt, t }: { attempt: RolloutAttempt; t: T }) {
  const [open, setOpen] = useState(false);
  const media = attempt.media;
  if (!media.available) {
    return (
      <p className="ar-muted" data-testid="rollout-video-missing">
        {t("automatic.rollouts.video.missing")} (
        {codeText("automatic.rollouts.video.reason", media.reason, t)})
      </p>
    );
  }
  const duration = attempt.seconds && attempt.seconds > 0 ? attempt.seconds : 1;
  return (
    <details
      className="ar-raw"
      data-testid="rollout-videos"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        {t("automatic.rollouts.video.open").replace(
          "{n}",
          String(media.videos.length),
        )}
      </summary>
      <p>
        <Link href={media.viewer} className="ds-focus">
          {t("automatic.rollouts.video.viewer")}
        </Link>
      </p>
      {open && (
        <div className="ar-rollout-player">
          <TimeProvider duration={duration}>
            <AnnotationsProvider>
              <SimpleVideosPlayer
                videosInfo={media.videos.map((v) => ({
                  filename: v.camera,
                  url: v.url,
                }))}
              />
              <PlaybackBar />
            </AnnotationsProvider>
          </TimeProvider>
        </div>
      )}
    </details>
  );
}
