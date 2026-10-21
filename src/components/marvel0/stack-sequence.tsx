"use client";
// The "Whole stack" block of the arm step: the two one-click buttons (Start
// whole stack, Stop whole stack), Cancel while one runs, and the progress of the
// guided start or stop the backend reports. A start lists its chain (M, marker,
// wait, FCI check, A, B, C) and a stop its terminals (C, A, B), each with how
// far it is; a step the sequence never reached after a failure reads "not
// reached". A failure says at which step and why, and that pressing the same
// button again skips what is already done. Every button that is off says why in
// the backend's words. The buttons only ask the caller to open a confirm
// window; nothing here talks to the backend.
import { Power, Rocket, X } from "lucide-react";
import { Badge, Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { WordedText } from "./action-parts";
import { wordOf } from "./ctl-text";
import type { Say } from "./marvel0-logic";
import { sayText } from "./marvel0-say";
import {
  canStartSequence,
  canStopSequence,
  startNeed,
  type ActView,
  type StartNeed,
  type SequenceStep,
  type SequenceWait,
} from "./stack-act-logic";
import {
  STEP_TONE,
  failedStep,
  shownState,
  stopSummary,
} from "./stack-sequence-logic";
import { Reasons } from "./stack-controls";
import type { DialogKind } from "./stack-dialog-logic";
import type { StackActions } from "./use-stack-actions";

/** The sentence of each reason the start of the whole stack is not offered. */
const NEEDS: Record<StartNeed, string> = {
  free: "marvel0.ctl.seq.needs_free",
  all_ready: "marvel0.ctl.seq.needs_all_ready",
  a_unfit: "marvel0.ctl.seq.needs_a_unfit",
};

/** Seconds as the page shows them: one decimal, no trailing zero. */
const secs = (value: number): number => Math.round(value * 10) / 10;

/** What the start before A waits for, one line each, with the numbers. */
function holdSay(code: string, wait: SequenceWait): Say {
  const min = secs(wait.minS ?? 0);
  switch (code) {
    case "marker_young":
      return {
        key: "marvel0.ctl.seq.hold.marker_young",
        params: { min, age: secs(wait.ageS ?? 0) },
      };
    case "heavy_jobs_running":
      return {
        key: "marvel0.ctl.seq.hold.heavy_jobs_running",
        params: {
          count: wait.heavy?.count ?? 0,
          cap: Math.round(wait.capS ?? 0),
          waited: Math.round(wait.waitedS ?? 0),
        },
      };
    case "heavy_jobs_unknown":
      return {
        key: "marvel0.ctl.seq.hold.heavy_jobs_unknown",
        params: { min },
      };
    default:
      return { key: `marvel0.ctl.seq.hold.${code}`, params: {} };
  }
}

export function SequenceBlock({
  act,
  actions,
  ask,
}: {
  act: ActView;
  actions: StackActions;
  ask: (want: DialogKind) => void;
}) {
  const { t } = useLocale();
  const seq = act.sequence;
  const stop = seq.kind === "stop";
  const live = seq.state === "running" || seq.state === "waiting_confirm";
  const tone =
    seq.state === "failed"
      ? "danger"
      : seq.state === "done"
        ? "success"
        : live
          ? "info"
          : "neutral";
  const idle = actions.working !== null || actions.locked;
  const canStopAll = canStopSequence(act);
  const failed = failedStep(seq);
  const labelOf = (item: SequenceStep): string =>
    stop
      ? wordOf(t, "marvel0.ctl.name.", item.terminal)
      : wordOf(t, "marvel0.ctl.seq.chain.", item.step);
  const stateWord = (item: SequenceStep): string =>
    wordOf(
      t,
      stop
        ? "marvel0.ctl.seq.step_state."
        : "marvel0.ctl.seq.start_step_state.",
      shownState(seq, item),
    );
  const summary = stop && seq.state === "done" ? stopSummary(seq) : null;
  const need = startNeed(act);
  // Where a start that runs on its own will stop for the person (the pause).
  const pausedAt = [seq.pauseB && "B", seq.pauseC && "C"]
    .filter(Boolean)
    .join(" / ");
  // The terminals are named by their letters, as the page's cards are.
  const names = (ids: string[]) => ids.join(", ");
  return (
    <div
      className="rg-seq"
      data-testid="marvel0-sequence"
      data-state={seq.state}
      data-kind={seq.kind}
    >
      <div className="rg-seq__head">
        <h4 className="rg-marker__title">{t("marvel0.ctl.seq.title")}</h4>
        <Badge tone={tone}>
          {wordOf(
            t,
            stop ? "marvel0.ctl.seq.stop_state." : "marvel0.ctl.seq.state.",
            seq.state,
          )}
        </Badge>
      </div>
      <p className="rg-muted">{t("marvel0.ctl.seq.lead")}</p>
      <p className="rg-muted">{t("marvel0.ctl.seq.lead_stop")}</p>
      <div className="rg-comp__buttons">
        <Button
          variant="primary"
          icon={Rocket}
          data-testid="marvel0-start-all"
          loading={actions.working === "sequence"}
          disabled={!canStartSequence(act) || idle}
          onClick={() => ask({ kind: "sequence" })}
        >
          {t("marvel0.ctl.seq.button")}
        </Button>
        <Button
          variant="secondary"
          className="rg-btn--danger-outline"
          icon={Power}
          data-testid="marvel0-stop-all"
          loading={actions.working === "sequence_stop"}
          disabled={!canStopAll || idle}
          onClick={() => ask({ kind: "sequence_stop" })}
        >
          {t("marvel0.ctl.seq.stop_button")}
        </Button>
        {live && (
          <Button
            variant="secondary"
            icon={X}
            data-testid="marvel0-sequence-cancel"
            disabled={actions.working !== null}
            loading={actions.working === "sequence:cancel"}
            onClick={() => void actions.cancelSequence()}
          >
            {t(stop ? "marvel0.ctl.seq.cancel_stop" : "marvel0.ctl.seq.cancel")}
          </Button>
        )}
      </div>
      {live && (
        <p className="rg-muted" data-testid="marvel0-sequence-cancel-note">
          {t(
            stop
              ? "marvel0.ctl.seq.cancel_note_stop"
              : "marvel0.ctl.seq.cancel_note",
          )}
        </p>
      )}
      {act.stopAll.offered && (
        <Reasons
          label="marvel0.ctl.off.stop_all"
          control={{
            enabled: act.stopAll.canStop,
            blockers: act.stopAll.blockers,
            needsOverride: false,
          }}
          testId="marvel0-block-stop-all"
          // A sequence that runs says so itself (its state, its steps): the
          // backend's "already in progress" would only repeat it.
          quiet={
            live &&
            act.stopAll.blockers.length > 0 &&
            act.stopAll.blockers.every((b) => b.code === "sequence_active")
          }
        />
      )}
      {seq.state === "running" && seq.step && (
        <p
          className="rg-working"
          role="status"
          data-testid="marvel0-sequence-step"
        >
          {sayText(
            {
              key: "marvel0.ctl.seq.step",
              params: {
                step: { raw: wordOf(t, "marvel0.ctl.seq.step.", seq.step) },
              },
            },
            t,
          )}
          {seq.step === "wait" && seq.remainingS !== null && (
            <>
              {" "}
              {sayText(
                {
                  key: "marvel0.ctl.marker.countdown",
                  params: { s: Math.ceil(seq.remainingS) },
                },
                t,
              )}
            </>
          )}
        </p>
      )}
      {seq.state === "running" &&
        !stop &&
        seq.wait &&
        seq.wait.holds.length > 0 && (
          // What the start waits for before A, and for how long: the marker's age,
          // heavy jobs that still run (their end is not known, the start ends if
          // they do not stop), the arm's sockets.
          <div data-testid="marvel0-sequence-waiting">
            <p className="rg-muted">{t("marvel0.ctl.seq.waiting_on")}</p>
            <ul className="rg-muted">
              {seq.wait.holds.map((code) => (
                <li key={code} data-hold={code}>
                  {sayText(holdSay(code, seq.wait!), t)}
                </li>
              ))}
            </ul>
          </div>
        )}
      {seq.state === "running" && !stop && seq.through === "c" && pausedAt && (
        <p className="rg-muted" data-testid="marvel0-sequence-paused-note">
          {sayText(
            {
              key: "marvel0.ctl.seq.paused_note",
              params: { parts: { raw: pausedAt } },
            },
            t,
          )}
        </p>
      )}
      {actions.answered?.key === "sequence" &&
        actions.answered.done === "already_started" && (
          <p
            className="rg-muted"
            role="status"
            data-testid="marvel0-sequence-already"
          >
            {t("marvel0.ctl.seq.already_started")}
          </p>
        )}
      {actions.answered?.key === "sequence_stop" &&
        actions.answered.done === "already_stopped" && (
          <p
            className="rg-muted"
            role="status"
            data-testid="marvel0-sequence-already"
          >
            {t("marvel0.ctl.seq.already_stopped")}
          </p>
        )}
      {seq.steps.length > 0 && (
        // Where the sequence is: every step in the order it runs, each with how
        // far it is, as the backend counts it ("not reached" where the
        // sequence ended before it).
        <ol className="rg-seq__steps" data-testid="marvel0-sequence-progress">
          {seq.steps.map((item) => {
            const shown = shownState(seq, item);
            return (
              <li
                key={item.step}
                data-step={item.step}
                data-comp={item.terminal}
                data-step-state={shown}
                aria-current={item.state === "running" ? "step" : undefined}
              >
                <span>{labelOf(item)}</span>{" "}
                <Badge tone={STEP_TONE[shown]}>{stateWord(item)}</Badge>
                {item.tookS !== null &&
                  shown !== "pending" &&
                  shown !== "not_reached" && (
                    // Where the time goes: what this step has taken.
                    <>
                      {" "}
                      <span
                        className="rg-muted"
                        data-testid="marvel0-step-took"
                        data-took={item.tookS}
                      >
                        {sayText(
                          {
                            key: "marvel0.ctl.seq.took",
                            params: { s: secs(item.tookS) },
                          },
                          t,
                        )}
                      </span>
                    </>
                  )}
              </li>
            );
          })}
        </ol>
      )}
      {seq.state === "waiting_confirm" && seq.next && (
        <p
          className="rg-seq__wait"
          role="status"
          data-testid="marvel0-sequence-wait"
        >
          {sayText(
            {
              key: "marvel0.ctl.seq.waiting_for",
              params: { name: { key: `marvel0.stack.name.${seq.next}` } },
            },
            t,
          )}
        </p>
      )}
      {(seq.state === "done" || seq.state === "failed") &&
        !stop &&
        seq.tookS !== null &&
        seq.tookS > 0 && (
          <p className="rg-muted" data-testid="marvel0-sequence-took">
            {sayText(
              {
                key: "marvel0.ctl.seq.took_total",
                params: { s: secs(seq.tookS) },
              },
              t,
            )}
          </p>
        )}
      {(seq.state === "failed" || seq.state === "cancelled") && seq.reason && (
        <p
          className={seq.state === "failed" ? "rg-bad" : "rg-muted"}
          role="status"
          data-testid="marvel0-sequence-reason"
        >
          {failed && (
            <>
              {sayText(
                {
                  key: "marvel0.ctl.seq.failed_at",
                  params: { step: { raw: labelOf(failed) } },
                },
                t,
              )}{" "}
            </>
          )}
          <WordedText worded={seq.reason} />
        </p>
      )}
      {seq.state === "failed" && (
        // Pressing the button again is the retry: what is ready or stopped is
        // skipped, so nothing is done twice.
        <p className="rg-muted" data-testid="marvel0-sequence-retry">
          {t(
            stop ? "marvel0.ctl.seq.retry_stop" : "marvel0.ctl.seq.retry_start",
          )}
        </p>
      )}
      {seq.state === "done" && stop && summary && (
        <p className="rg-ok" role="status" data-testid="marvel0-sequence-done">
          {summary.stopped.length > 0 && (
            <>
              {sayText(
                {
                  key: "marvel0.ctl.seq.stop_done_stopped",
                  params: { names: { raw: names(summary.stopped) } },
                },
                t,
              )}{" "}
            </>
          )}
          {summary.already.length > 0 && (
            <>
              {sayText(
                {
                  key: "marvel0.ctl.seq.stop_done_already",
                  params: { names: { raw: names(summary.already) } },
                },
                t,
              )}{" "}
            </>
          )}
          {t("marvel0.ctl.seq.stop_done")}
        </p>
      )}
      {seq.state === "done" && !stop && (
        <p className="rg-ok" role="status" data-testid="marvel0-sequence-done">
          {t("marvel0.ctl.seq.start_done")}
        </p>
      )}
      {need && !live && (
        <p className="rg-muted" data-testid="marvel0-sequence-needs">
          {t(NEEDS[need])}
        </p>
      )}
    </div>
  );
}
