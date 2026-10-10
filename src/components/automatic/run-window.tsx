"use client";
// The window of one run: the 13-state graph, the current episode and counters,
// what the run needs from a person, the event timeline, the metrics and a
// stop button. Everything on it comes from the run's own files through the
// read-only API; the only writes are a person's (stop, label, resume, scene
// answer), each behind its own control.
import Link from "next/link";
import { useRef, useState } from "react";
import { ArrowLeft, CircleAlert } from "lucide-react";
import { Badge, Button, Progress, useConfirm } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { automaticApi, newRequestId } from "./api";
import { errorText } from "./launch-panel";
import { PendingCard } from "./pending-card";
import { isBlind, mergeCard, resultDone, resultKey } from "./run-logic";
import { ModeChips, Note, StateGraph, stateName } from "./run-parts";
import { useRunEvents, useRunMetrics, useRunSnapshot } from "./run-poll";
import { RunMetrics } from "./run-metrics";
import { RunTimeline } from "./run-timeline";
import type { PendingCard as PendingCardData } from "./types";

export function RunWindow({ runId }: { runId: string }) {
  const { t } = useLocale();
  const poll = useRunSnapshot(runId);
  const snapshot = poll.data;
  const events = useRunEvents(runId, snapshot?.seq);
  const metrics = useRunMetrics(runId, snapshot?.seq);
  const { confirm, dialog } = useConfirm();
  const [posted, setPosted] = useState<PendingCardData | null>(null);
  const [stopping, setStopping] = useState(false);
  const [notice, setNotice] = useState("");
  const stopId = useRef("");
  const busy = useRef(false);

  const card = mergeCard(snapshot?.pending_card ?? null, posted);
  const blind = isBlind(card);

  const stop = async () => {
    if (!snapshot || busy.current) return;
    const ok = await confirm({
      title: t("automatic.run.stop.title"),
      description: t("automatic.run.stop.detail"),
      confirmLabel: t("automatic.run.stop.button"),
      tone: "danger",
    });
    if (!ok || busy.current) return;
    busy.current = true;
    setStopping(true);
    setNotice("");
    // One command id per stop attempt: a lost answer is retried as the same
    // command, a refused one starts afresh.
    if (!stopId.current) stopId.current = newRequestId();
    try {
      const got = await automaticApi.stop(runId, stopId.current);
      if (got.result === "refused") stopId.current = "";
      const key = resultKey(resultDone(got) ? null : got);
      setNotice(t(key ?? "automatic.run.stop.sent"));
      if (resultDone(got)) stopId.current = "";
      poll.refresh();
    } catch (caught) {
      setNotice(errorText(caught, t, STOP_ERRORS));
    } finally {
      busy.current = false;
      setStopping(false);
    }
  };

  // The run has not written its first file yet (404): it is starting.
  const starting = !snapshot && poll.status === 404;
  const finished = snapshot?.state === "COMPLETED";

  return (
    <main className="ds-root pg-workbench ar-page">
      <div className="ar-head">
        <div>
          <Link href="/automatic" className="ds-focus ar-link">
            <ArrowLeft width={16} height={16} aria-hidden="true" />
            {t("automatic.run.window.back")}
          </Link>
          <h1>{t("automatic.run.window.title")}</h1>
          <p className="ar-code">{runId}</p>
        </div>
        {snapshot && !finished && (
          <Button
            variant="danger"
            loading={stopping}
            disabled={!snapshot.runner.alive}
            aria-describedby={
              !snapshot.runner.alive ? "ar-stop-why" : undefined
            }
            onClick={() => void stop()}
          >
            {t("automatic.run.stop.button")}
          </Button>
        )}
      </div>
      {snapshot && !snapshot.runner.alive && !finished && (
        <p className="ar-muted" id="ar-stop-why">
          {t("automatic.run.stop.not_running")}
        </p>
      )}
      {notice && (
        <Note tone="info" role="status">
          {notice}
        </Note>
      )}
      {poll.failures > 0 && !starting && (
        <Note tone="warning" role="status" icon={CircleAlert}>
          {t("automatic.run.window.failed")} ({poll.error})
        </Note>
      )}
      {starting && (
        <Note tone="info" role="status">
          {t("automatic.run.window.starting")}
        </Note>
      )}
      {!snapshot && !starting && poll.failures === 0 && (
        <p className="ar-muted">{t("automatic.run.loading")}</p>
      )}

      {snapshot && (
        <>
          <section className="ar-section" aria-labelledby="ar-now-title">
            <h2 id="ar-now-title">{t("automatic.run.now.title")}</h2>
            <div className="ar-chips">
              <ModeChips
                execution={snapshot.execution_mode}
                reset={snapshot.reset_mode}
              />
              <Badge
                tone={
                  snapshot.state === "FAULT_LOCKED"
                    ? "danger"
                    : snapshot.state === "WAIT_HUMAN"
                      ? "warning"
                      : finished
                        ? "success"
                        : "info"
                }
              >
                {stateName(snapshot.state, t)}
              </Badge>
              <Badge tone={snapshot.runner.alive ? "success" : "neutral"}>
                {snapshot.runner.alive
                  ? t("automatic.run.runner.alive")
                  : t("automatic.run.runner.stopped")}
              </Badge>
            </div>
            <Progress
              value={snapshot.episodes.done}
              max={Math.max(snapshot.episodes.total, 1)}
              label={t("automatic.run.list.progress")
                .replace("{done}", String(snapshot.episodes.done))
                .replace("{total}", String(snapshot.episodes.total))}
            />
            <dl className="ar-dl">
              {snapshot.episodes.current && (
                <>
                  <dt>{t("automatic.run.now.episode")}</dt>
                  <dd>
                    {snapshot.episodes.current.no ??
                      snapshot.episodes.current.episode_id ??
                      "—"}
                    {snapshot.episodes.current.step != null &&
                      ` · ${t("automatic.run.now.step")} ${snapshot.episodes.current.step}${
                        snapshot.episodes.current.max_steps != null
                          ? ` / ${snapshot.episodes.current.max_steps}`
                          : ""
                      }`}
                  </dd>
                </>
              )}
              <dt>{t("automatic.run.now.planned")}</dt>
              <dd>{snapshot.counters.planned_interventions}</dd>
              <dt>{t("automatic.run.now.unplanned")}</dt>
              <dd>{snapshot.counters.unplanned_interventions}</dd>
              <dt>{t("automatic.run.now.faults")}</dt>
              <dd>{snapshot.counters.faults}</dd>
            </dl>
          </section>

          {card && (
            <PendingCard
              runId={runId}
              card={card}
              challenge={snapshot.challenge}
              onLabelled={setPosted}
              onChanged={poll.refresh}
            />
          )}

          <section className="ar-section" aria-labelledby="ar-graph-title">
            <h2 id="ar-graph-title">{t("automatic.run.graph")}</h2>
            {snapshot.reset_mode === "human_assisted" && (
              <p className="ar-muted">{t("automatic.run.graph.manual_note")}</p>
            )}
            <StateGraph
              state={snapshot.state}
              resetMode={snapshot.reset_mode}
            />
          </section>

          <section className="ar-section" aria-labelledby="ar-events-title">
            <h2 id="ar-events-title">{t("automatic.run.timeline.title")}</h2>
            <RunTimeline events={events} blind={blind} />
          </section>

          <section className="ar-section" aria-labelledby="ar-metrics-title">
            <h2 id="ar-metrics-title">{t("automatic.run.metrics.title")}</h2>
            {metrics.error && (
              <p className="ar-muted">
                {t("automatic.run.metrics.failed")} ({metrics.error})
              </p>
            )}
            {metrics.report && (
              <RunMetrics report={metrics.report} blind={blind} />
            )}
          </section>
        </>
      )}
      {dialog}
    </main>
  );
}

const STOP_ERRORS: Record<string, string> = {
  not_running: "automatic.run.result.not_running",
};
