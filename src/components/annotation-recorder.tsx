"use client";
/**
 * Two controls above the annotation editor:
 *
 * - Confirm episode complete: a person's statement that this episode's
 *   subtask annotation is done (saved first if there are unsaved edits).
 * - Start / Stop recording: times a stretch of annotation work. Stopping
 *   writes `eval/<dataset>_human_<hour>.md` with the episodes confirmed while
 *   it ran, their subtask counts, second/frame coverage, semantic richness,
 *   structural correctness and agreement with same-content copies — the same
 *   record an agent's full-dataset task produces, so the two compare.
 *
 * The session lives on the server, so a reload or a second tab keeps it.
 */
import { useCallback, useEffect, useState } from "react";
import { useConfirmAction } from "@/components/shell/confirm";
import { useAnnotations } from "@/context/annotations-context";
import {
  cancelRecording,
  fetchEpisodeStatus,
  fetchRecording,
  saveEpisodeStatus,
  startRecording,
  stopRecording,
  type EpisodeStatus,
  type RecordingResult,
  type RecordingSession,
} from "@/utils/annotationsClient";
import { T, useLocale } from "./levi-locale";
import { CircleCheck, Circle, Square, Trash2 } from "lucide-react";
import { Button, Tooltip } from "@/components/ds";

function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

export default function AnnotationRecorder() {
  const { t } = useLocale();
  const confirm = useConfirmAction();
  const { episodeId, ident, dirty, save, backendEnabled } = useAnnotations();
  const [session, setSession] = useState<RecordingSession | null>(null);
  const [status, setStatus] = useState<Record<string, EpisodeStatus>>({});
  const [now, setNow] = useState(() => Date.now() / 1000);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<RecordingResult | null>(null);
  const identKey = `${ident.repoId ?? ""}|${ident.localPath ?? ""}`;

  const refresh = useCallback(async () => {
    if (!backendEnabled || (!ident.repoId && !ident.localPath)) return;
    const [current, done] = await Promise.all([
      fetchRecording(ident),
      fetchEpisodeStatus(ident),
    ]);
    setSession(current);
    setStatus(done);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identKey, backendEnabled]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!session) return;
    const id = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(id);
  }, [session]);

  async function act(step: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await step();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!backendEnabled || episodeId === null) return null;
  const confirmed = status[String(episodeId)];
  const confirmedInSession = session
    ? Object.values(status).filter((s) => s.confirmed_at >= session.started_at)
        .length
    : 0;

  const confirmEpisode = () =>
    void act(async () => {
      if (!confirmed && dirty) {
        const saved = await save();
        if (!saved.ok) throw new Error(saved.error || t("Save failed"));
      }
      const value = await saveEpisodeStatus(episodeId, ident, !confirmed);
      setStatus((prev) => {
        const next = { ...prev };
        if (value) next[String(episodeId)] = value;
        else delete next[String(episodeId)];
        return next;
      });
    });

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      data-testid="annotation-recorder"
    >
      <Tooltip
        content={t(
          "States that this episode's subtask annotation is complete; the recorder counts it.",
        )}
      >
        <Button
          size="sm"
          icon={CircleCheck}
          disabled={busy}
          aria-pressed={Boolean(confirmed)}
          onClick={confirmEpisode}
        >
          {t(
            confirmed ? "Episode confirmed (undo)" : "Confirm episode complete",
          )}
        </Button>
      </Tooltip>
      {session ? (
        <>
          <Button
            size="sm"
            variant="primary"
            icon={Square}
            disabled={busy}
            onClick={() =>
              void act(async () => {
                if (dirty) await save();
                const value = await stopRecording(ident);
                setSession(null);
                setResult(value);
              })
            }
          >
            {t("Stop recording")} ·{" "}
            <span className="tabular">{clock(now - session.started_at)}</span>
          </Button>
          <span className="vw-muted text-xs tabular">
            {t("Confirmed this session")}: {confirmedInSession}
          </span>
          <Button
            size="sm"
            variant="ghost"
            icon={Trash2}
            disabled={busy}
            onClick={async () => {
              if (
                !(await confirm({
                  title: t("Discard this recording without a record?"),
                  confirmLabel: t("Discard"),
                  tone: "danger",
                }))
              )
                return;
              void act(async () => {
                await cancelRecording(ident);
                setSession(null);
              });
            }}
          >
            {t("Discard")}
          </Button>
        </>
      ) : (
        <Tooltip
          content={t(
            "Times your annotation work; Stop writes an evaluation record to .state/eval.",
          )}
        >
          <Button
            size="sm"
            icon={Circle}
            disabled={busy}
            onClick={() =>
              void act(async () => {
                setResult(null);
                setSession(await startRecording(ident));
                setNow(Date.now() / 1000);
              })
            }
          >
            {t("Start recording")}
          </Button>
        </Tooltip>
      )}
      {result && (
        <span className="vw-muted text-xs" role="status">
          <T>Recorded</T> {clock(result.seconds)} · {result.episodes}{" "}
          <T>episodes</T> · {result.segments} <T>subtasks</T> · <T>coverage</T>{" "}
          {(100 * result.coverage_seconds).toFixed(1)}% ·{" "}
          <code>{result.path.split("/").slice(-2).join("/")}</code>
        </span>
      )}
      {error && (
        <span className="text-xs text-(--ds-danger)" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
