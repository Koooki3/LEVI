"use client";
// Step 1 of the marvel0 page: the arm stack. One card per terminal in the
// order they come up (M, Desk, A, B, C): the status card the passive probes
// feed, with the terminal's controls under its summary (stack-controls.tsx),
// Desk read-only because a person does it in Franka Desk. Above the cards: the
// guided "Start whole stack" button and the arm-controller marker with its
// wait; under them the passive evidence folded away.
//
// The page reads the stack every 2 s and a click is "working" from the moment
// it is made until the next read has come back. Every button that is off says
// why in the backend's words; the page does not work a reason out itself.
// What the gates are worth is said where they are used: they stop mistakes,
// not another program of the same account; Desk and the physical emergency
// stop are the barrier.
import { useEffect, useState } from "react";
import { CircleAlert, CircleX, RefreshCw, Rocket, X } from "lucide-react";
import { Badge, Button, Card } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Notice } from "./notice";
import { WordedText } from "./action-parts";
import { nodeAnchor, type ChainId } from "./chain-logic";
import { wordOf } from "./ctl-text";
import { LiveAge, useNow } from "./live-age";
import { Marvel0CardView, StepHeading } from "./marvel0-card";
import type { Marvel0Card, Marvel0CardId, Marvel0State } from "./marvel0-logic";
import { sayText } from "./marvel0-say";
import {
  STATE_TONE,
  canStartSequence,
  gripperTestFailed,
  markerRemaining,
  operationRunning,
  stateKey,
  type ActView,
  type CompState,
  type CompId,
} from "./stack-act-logic";
import type { StackApi } from "./stack-api";
import { CompControls, GripperTest } from "./stack-controls";
import { StackDialog } from "./stack-dialog";
import { dialogSpec, type DialogKind } from "./stack-dialog-logic";
import { StackEvidence } from "./stack-evidence";
import type { Polled } from "./use-polled";
import { useStackActions, type StackActions } from "./use-stack-actions";
import { STACK_FRESH_MS, type StackDoc } from "./use-stack-poll";

const COMP_OF: Partial<Record<Marvel0CardId, CompId>> = {
  monitor: "M",
  arm: "A",
  gripper: "B",
  robot_server: "C",
};
const NODE_OF: Partial<Record<Marvel0CardId, ChainId>> = {
  monitor: "M",
  desk: "desk",
  arm: "A",
  gripper: "B",
  robot_server: "C",
};
/** The coloured edge of a card by the state of its terminal. */
const EDGE: Record<CompState, Marvel0State | "working"> = {
  idle: "not_started",
  starting: "working",
  ready: "ok",
  failed: "red",
  dead: "red",
  unknown: "unknown",
  foreign_running: "stale",
  stopping: "working",
};
/** A dialog that stays open on these refusals: the person fixes the input. */
const STAYS_OPEN = new Set([
  "client_connected",
  "confirm_required",
  "attest_missing",
]);
/** A failed operation is shown for this long after it ended. */
const FAILED_SHOWN_S = 180;

/** Whether the backend still offers what a dialog was opened for. */
function stillOffered(want: DialogKind, act: ActView): boolean {
  switch (want.kind) {
    case "start":
      return act.comps[want.comp].canStart;
    case "stop":
      return (
        act.comps[want.comp].canStop || act.comps[want.comp].canStopWithOverride
      );
    case "gripper":
      return act.gripper.canRun;
    case "marker":
      return want.op === "adopt"
        ? act.marker.canAdopt
        : want.op === "release"
          ? act.marker.canRelease
          : act.marker.canTouch;
    case "sequence":
      return canStartSequence(act);
  }
}

function SequenceBlock({
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
  const live = seq.state === "running" || seq.state === "waiting_confirm";
  const tone =
    seq.state === "failed"
      ? "danger"
      : seq.state === "done"
        ? "success"
        : live
          ? "info"
          : "neutral";
  return (
    <div
      className="rg-seq"
      data-testid="marvel0-sequence"
      data-state={seq.state}
    >
      <div className="rg-seq__head">
        <h4 className="rg-marker__title">{t("marvel0.ctl.seq.title")}</h4>
        <Badge tone={tone}>
          {wordOf(t, "marvel0.ctl.seq.state.", seq.state)}
        </Badge>
      </div>
      <p className="rg-muted">{t("marvel0.ctl.seq.lead")}</p>
      <div className="rg-comp__buttons">
        <Button
          variant="primary"
          icon={Rocket}
          disabled={
            !canStartSequence(act) || actions.working !== null || actions.locked
          }
          onClick={() => ask({ kind: "sequence" })}
        >
          {t("marvel0.ctl.seq.button")}
        </Button>
        {live && (
          <Button
            variant="secondary"
            icon={X}
            disabled={actions.working !== null}
            loading={actions.working === "sequence:cancel"}
            onClick={() => void actions.cancelSequence()}
          >
            {t("marvel0.ctl.seq.cancel")}
          </Button>
        )}
      </div>
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
      {(seq.state === "failed" || seq.state === "cancelled") && seq.reason && (
        <p
          className={seq.state === "failed" ? "rg-bad" : "rg-muted"}
          role="status"
          data-testid="marvel0-sequence-reason"
        >
          <WordedText worded={seq.reason} />
        </p>
      )}
      {!canStartSequence(act) && seq.state === "idle" && !live && (
        <p className="rg-muted" data-testid="marvel0-sequence-needs">
          {t(
            operationRunning(act)
              ? "marvel0.ctl.seq.needs_free"
              : "marvel0.ctl.seq.needs_idle",
          )}
        </p>
      )}
    </div>
  );
}

function MarkerBlock({
  act,
  lastOk,
  actions,
  ask,
}: {
  act: ActView;
  lastOk: number | null;
  actions: StackActions;
  ask: (want: DialogKind) => void;
}) {
  const { t } = useLocale();
  const now = useNow();
  const marker = act.marker;
  const since = lastOk === null ? 0 : Math.max(0, now - lastOk) / 1000;
  const remaining = markerRemaining(marker, since);
  const busy =
    actions.working !== null || actions.locked || operationRunning(act);
  return (
    <div
      className="rg-marker"
      data-testid="marvel0-marker"
      data-state={marker.state}
    >
      <div className="rg-marker__head">
        <h4 className="rg-marker__title">{t("marvel0.ctl.marker.title")}</h4>
        <Badge
          tone={
            marker.state === "present"
              ? remaining !== null && remaining > 0
                ? "info"
                : "success"
              : "neutral"
          }
        >
          {wordOf(t, "marvel0.ctl.marker.state.", marker.state)}
        </Badge>
      </div>
      <p className="rg-muted">
        {marker.state === "present" && marker.ageS !== null
          ? sayText(
              {
                key: "marvel0.ctl.marker.set_ago",
                params: {
                  age: { ago: marker.ageS + since },
                  who: {
                    key:
                      marker.ours === true
                        ? "marvel0.ctl.marker.ours"
                        : marker.ours === false
                          ? "marvel0.ctl.marker.not_ours"
                          : "marvel0.ctl.marker.who_unknown",
                  },
                },
              },
              t,
            )
          : t("marvel0.ctl.marker.why")}
      </p>
      {remaining !== null && (
        <p
          className={remaining > 0 ? "rg-working" : "rg-ok"}
          role="status"
          data-testid="marvel0-marker-countdown"
          data-remaining={Math.ceil(remaining)}
        >
          {remaining > 0
            ? sayText(
                {
                  key: "marvel0.ctl.marker.countdown",
                  params: { s: Math.ceil(remaining) },
                },
                t,
              )
            : t("marvel0.ctl.marker.wait_over")}
        </p>
      )}
      <div className="rg-comp__buttons">
        {marker.canTouch && (
          <Button
            disabled={busy}
            onClick={() => ask({ kind: "marker", op: "touch" })}
          >
            {t("marvel0.ctl.btn.marker_touch")}
          </Button>
        )}
        {marker.canAdopt && (
          <Button
            disabled={busy}
            onClick={() => ask({ kind: "marker", op: "adopt" })}
          >
            {t("marvel0.ctl.btn.marker_adopt")}
          </Button>
        )}
        {marker.canRelease && (
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => ask({ kind: "marker", op: "release" })}
          >
            {t("marvel0.ctl.btn.marker_release")}
          </Button>
        )}
      </div>
    </div>
  );
}

/** The backend's own operation (a start waiting for its units, a check, the
 * gripper test) and the notices it attaches to the stack. */
function Operation({ act }: { act: ActView }) {
  const { t } = useLocale();
  const now = useNow();
  const op = act.operation;
  const failed =
    op?.state === "failed" &&
    op.finishedAt !== null &&
    now / 1000 - op.finishedAt <= FAILED_SHOWN_S;
  return (
    <>
      {op?.state === "running" && (
        <p className="rg-working" role="status" data-testid="marvel0-operation">
          {sayText(
            {
              key: "marvel0.ctl.op.running",
              params: { what: { raw: wordOf(t, "marvel0.ctl.op.", op.kind) } },
            },
            t,
          )}
        </p>
      )}
      {op && failed && (
        <p
          className="rg-bad rg-op-failed"
          role="status"
          data-testid="marvel0-operation-failed"
        >
          <CircleX size={14} aria-hidden="true" />
          {sayText(
            {
              key: "marvel0.ctl.op.failed",
              params: {
                what: { raw: wordOf(t, "marvel0.ctl.op.", op.kind) },
                code: {
                  raw: op.code
                    ? wordOf(t, "marvel0.ctl.opcode.", op.code)
                    : "?",
                },
              },
            },
            t,
          )}
        </p>
      )}
      {act.notices.map((notice) => (
        <Notice
          key={notice.code + JSON.stringify(notice.params)}
          tone="warning"
          testId="marvel0-notice"
          code={notice.code}
        >
          {sayText(
            {
              key: `marvel0.ctl.notice.${notice.code.replace(/^notice\./, "")}`,
              params: notice.params,
            },
            t,
          )}
        </Notice>
      ))}
    </>
  );
}

export function StackPanel({
  api,
  poll,
  cards,
}: {
  api: StackApi;
  poll: Polled<StackDoc>;
  /** The step's status cards in the order M, Desk, A, B, C. */
  cards: Marvel0Card[];
}) {
  const { t } = useLocale();
  const actions = useStackActions(api, poll);
  const [want, setWant] = useState<DialogKind | null>(null);
  const doc = poll.value;
  const act = doc?.act ?? null;
  const passive = doc?.passive ?? null;
  const spec = want ? dialogSpec(want, act) : null;
  const open = (next: DialogKind) => {
    actions.clear();
    setWant(next);
  };
  // A dialog confirms the state it was opened on: when the page can no longer
  // read the stack, or the backend stops offering the action, it closes.
  const lost =
    want !== null && (!poll.current || !act || !stillOffered(want, act));
  useEffect(() => {
    if (lost && actions.working === null) setWant(null);
  }, [lost, actions.working]);

  const send = async (typed: string, override: string) => {
    if (!want || !spec) return;
    const outcome = await actions.confirm(want, spec, typed, override);
    if (outcome.ok || (outcome.code && !STAYS_OPEN.has(outcome.code)))
      setWant(null);
  };

  return (
    <section
      className="rg-section rg-step"
      aria-labelledby="marvel0-step-arm"
      data-testid="marvel0-step-arm"
    >
      <div className="rg-step__head">
        <StepHeading n={1} id="marvel0-step-arm">
          {t("marvel0.step.arm")}
        </StepHeading>
        <span className="rg-step__tools">
          <LiveAge
            lastOk={poll.lastOk}
            warnAfterMs={STACK_FRESH_MS / 2}
            testId="marvel0-stack-age"
          />
          <Button
            size="sm"
            variant="ghost"
            icon={RefreshCw}
            onClick={() => void poll.refresh()}
          >
            {t("Refresh")}
          </Button>
        </span>
      </div>
      <p className="rg-step__lead">{t("marvel0.step.arm_lead")}</p>

      {!doc && poll.loaded && (
        <Notice tone="warning" role="status" testId="marvel0-stack-unreadable">
          {t("marvel0.ctl.unreadable")}
        </Notice>
      )}
      {doc && !poll.current && (
        <Notice tone="warning" role="status" testId="marvel0-stack-stale">
          {t("marvel0.ctl.stale")}
        </Notice>
      )}
      {passive && !passive.real && (
        <Notice tone="info" testId="marvel0-stack-fake">
          {t("marvel0.stack.fake")}
        </Notice>
      )}
      {doc && !act && (
        <Notice tone="info" testId="marvel0-stack-noact">
          {t("marvel0.ctl.no_act")}
        </Notice>
      )}
      {act && !act.enabled && (
        <Notice tone="info" testId="marvel0-stack-disabled">
          {t("marvel0.ctl.disabled")}
        </Notice>
      )}
      {passive?.notices
        .filter((n) => "key" in n && n.key === "marvel0.stack.logout")
        .map((n, index) => (
          <Notice key={index} tone="warning" testId="marvel0-stack-logout">
            {sayText(n, t)}
          </Notice>
        ))}

      {act && (
        <>
          <Operation act={act} />
          <Card
            padding="compact"
            className="rg-toolbar"
            data-testid="marvel0-stack-toolbar"
          >
            <SequenceBlock act={act} actions={actions} ask={open} />
            <MarkerBlock
              act={act}
              lastOk={poll.lastOk}
              actions={actions}
              ask={open}
            />
          </Card>
        </>
      )}

      <div className="rg-grid rg-grid--stack">
        {cards.map((card) => {
          const comp = COMP_OF[card.id];
          const node = NODE_OF[card.id];
          const view = comp && act ? act.comps[comp] : null;
          // B is up but its last gripper test failed: not a green "Ready".
          const testFailed =
            comp === "B" &&
            view?.state === "ready" &&
            act !== null &&
            gripperTestFailed(act.gripper);
          return (
            <Marvel0CardView
              key={card.id}
              card={card}
              domId={node ? nodeAnchor(node) : undefined}
              edge={
                view ? (testFailed ? "stale" : EDGE[view.state]) : undefined
              }
              badge={
                view ? (
                  testFailed ? (
                    <Badge tone="warning" icon={CircleAlert}>
                      {t("marvel0.chain.state.ready_test_failed")}
                    </Badge>
                  ) : (
                    <Badge tone={STATE_TONE[view.state]}>
                      {t(stateKey(view.state))}
                    </Badge>
                  )
                ) : undefined
              }
              controls={
                comp && view && act ? (
                  <>
                    <CompControls
                      comp={view}
                      act={act}
                      actions={actions}
                      lastOk={poll.lastOk}
                      ask={open}
                      next={
                        act.sequence.state === "waiting_confirm" &&
                        act.sequence.next === comp
                      }
                    />
                    {comp === "B" && (
                      <GripperTest act={act} actions={actions} ask={open} />
                    )}
                  </>
                ) : card.id === "desk" ? (
                  <p className="rg-muted" data-testid="marvel0-desk-readonly">
                    {t("marvel0.ctl.desk.by_hand")}
                  </p>
                ) : undefined
              }
            />
          );
        })}
      </div>
      {passive && <StackEvidence view={passive} />}
      <p className="rg-muted" data-testid="marvel0-stack-honest">
        {t("marvel0.ctl.honest")}
      </p>
      <StackDialog
        spec={spec}
        working={actions.working !== null}
        refused={want ? actions.refused : null}
        onClose={() => setWant(null)}
        onConfirm={(typed, override) => void send(typed, override)}
      />
    </section>
  );
}
