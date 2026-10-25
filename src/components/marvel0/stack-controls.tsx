"use client";
// The controls inside the card of one terminal (M, A, B, C): its state in
// words, why it is in that state, the ready lines seen and still missing while
// it starts, a Start and a Stop button each with the backend's own reason
// beside it when it is off, and the terminal's extras (A's FCI check, the
// checks before C, B's gripper test). What each shows is decided in
// stack-act-logic.ts; a click only asks the caller to open a dialog or run a
// check. Gripper buttons dispatch one explicitly clicked action through the
// shared action hook; the other controls retain their confirmation dialogs.
import {
  Check,
  CircleDashed,
  Hand,
  Lock,
  MoveHorizontal,
  Play,
  ShieldCheck,
  Square,
} from "lucide-react";
import { Button, Progress } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { RefusedBlock, WordedText } from "./action-parts";
import { stopping, wordOf } from "./ctl-text";
import { useNow } from "./live-age";
import { sayText } from "./marvel0-say";
import {
  canCheckFci,
  canCheckServer,
  gripperOutcome,
  operationRunning,
  phraseFor,
  readinessProgress,
  startControl,
  stopControl,
  type ActView,
  type CompState,
  type CompView,
  type Control,
  type GripperLast,
} from "./stack-act-logic";
import type { DialogKind } from "./stack-dialog-logic";
import { workKey, type StackActions } from "./use-stack-actions";

/** The stops of the terminals the arm depends on (A holds the arm link, C
 * drives the arm) are outlined in the danger colour and say what is left
 * behind in the label. HOLD is the only solid danger button of the page. */
const DANGEROUS_STOP = {
  A: "marvel0.ctl.btn.stop_A",
  C: "marvel0.ctl.btn.stop_C",
};

export interface ControlProps {
  comp: CompView;
  act: ActView;
  actions: StackActions;
  /** Epoch milliseconds of the read behind `act` (the waited seconds count
   * on from it). */
  lastOk: number | null;
  /** Opens a confirmation dialog. */
  ask: (want: DialogKind) => void;
  /** The guided start waits for the person to start this terminal. */
  next: boolean;
}

/** Whether the state explains by itself why a button is off. */
const SELF_EXPLAINING: readonly CompState[] = ["ready", "starting", "stopping"];
/** The states in which there is something of this page's to stop (or that it
 * cannot stop, for a reason worth saying). */
const STOPPABLE: readonly CompState[] = [
  "starting",
  "ready",
  "failed",
  "dead",
  "stopping",
  "foreign_running",
];

export function Reasons({
  label,
  control,
  testId,
  quiet = false,
}: {
  label: string;
  control: Control;
  testId: string;
  /** The state already says why the button is off. */
  quiet?: boolean;
}) {
  const { t } = useLocale();
  if (control.enabled || quiet) return null;
  const shown = stopping(control.blockers);
  // The first reason is the one to act on (the backend lists them in the
  // order things come up); the others are one click away, not off the page.
  const [first, ...rest] = shown;
  const item = (blocker: (typeof shown)[number]) => (
    <li key={blocker.code} data-code={blocker.code}>
      <WordedText worded={blocker.worded} />
    </li>
  );
  return (
    <div className="rg-block" data-testid={testId}>
      <span className="rg-block__label">
        <Lock size={12} aria-hidden="true" />
        {t(label)}
      </span>
      {!first ? (
        <span> {t("marvel0.ctl.no_reason")}</span>
      ) : (
        <>
          <ul className="rg-block__list">{item(first)}</ul>
          {rest.length > 0 && (
            <details className="rg-block__more">
              <summary>
                {t("marvel0.ctl.off.more").replace("{n}", String(rest.length))}
              </summary>
              <ul className="rg-block__list">{rest.map(item)}</ul>
            </details>
          )}
        </>
      )}
    </div>
  );
}

/** The ready lines of a starting terminal: how many are seen, which, and how
 * long the start has been waiting. */
function Readiness({
  comp,
  lastOk,
}: {
  comp: CompView;
  lastOk: number | null;
}) {
  const { t } = useLocale();
  const now = useNow();
  const progress = readinessProgress(comp.readiness);
  const r = comp.readiness;
  if (!r || !progress) return null;
  const waited =
    r.waitedS === null
      ? null
      : r.waitedS +
        (comp.state === "starting" && lastOk
          ? Math.max(0, now - lastOk) / 1000
          : 0);
  const lines = (
    <ul className="rg-ready__lines">
      {r.seen.map((line) => (
        <li
          key={`s-${line.key}`}
          className="rg-ready__seen"
          data-key={line.key}
        >
          <Check size={14} aria-hidden="true" />
          <span className="ds-sr-only">{t("marvel0.ctl.ready.seen")}</span>
          <WordedText worded={line.worded} />
        </li>
      ))}
      {r.missing.map((line) => (
        <li
          key={`m-${line.key}`}
          className="rg-ready__missing"
          data-key={line.key}
        >
          <CircleDashed size={14} aria-hidden="true" />
          <span className="ds-sr-only">{t("marvel0.ctl.ready.missing")}</span>
          <WordedText worded={line.worded} />
        </li>
      ))}
    </ul>
  );
  const label = sayText(
    {
      key: "marvel0.ctl.ready.progress",
      params: { seen: progress.seen, total: progress.total },
    },
    t,
  );
  // A terminal that is ready keeps its evidence one click away; one that is
  // starting or failed shows it, with the wait so far.
  if (comp.state === "ready")
    return (
      <details className="rg-ready" data-testid={`marvel0-ready-${comp.id}`}>
        <summary>{label}</summary>
        {lines}
      </details>
    );
  return (
    <div className="rg-ready" data-testid={`marvel0-ready-${comp.id}`}>
      <Progress value={progress.seen} max={progress.total} label={label} />
      {lines}
      {waited !== null && comp.state === "starting" && (
        <p className="rg-muted" data-testid={`marvel0-waited-${comp.id}`}>
          {sayText(
            r.timeoutS === null
              ? {
                  key: "marvel0.ctl.ready.waited",
                  params: { s: Math.floor(waited) },
                }
              : {
                  key: "marvel0.ctl.ready.waited_of",
                  params: {
                    s: Math.floor(waited),
                    max: Math.round(r.timeoutS),
                  },
                },
            t,
          )}
        </p>
      )}
    </div>
  );
}

/** What A's FCI check last found. */
function FciLine({ act }: { act: ActView }) {
  const { t } = useLocale();
  const fci = act.fci;
  if (!fci)
    return <p className="rg-muted">{t("marvel0.ctl.check.fci_none")}</p>;
  const open = fci.state === "open";
  const key = `marvel0.ctl.check.fci_${open ? "open" : fci.state === "closed" ? "closed" : "unreachable"}`;
  return (
    <p
      className={open && fci.fresh ? "rg-ok" : "rg-bad"}
      data-testid="marvel0-check-fci"
      data-state={fci.state}
      data-fresh={String(fci.fresh)}
    >
      {sayText({ key, params: { ago: { ago: fci.ageS } } }, t)}
      {open && !fci.fresh && <> {t("marvel0.ctl.check.too_old")}</>}
    </p>
  );
}

/** What the checks before C last found: each of the three, passed or not. */
function ServerLine({ act }: { act: ActView }) {
  const { t } = useLocale();
  const check = act.server;
  if (!check)
    return <p className="rg-muted">{t("marvel0.ctl.check.c_none")}</p>;
  const rows: Array<[string, boolean | null]> = [
    ["qos", check.qos],
    ["controllers", check.controllers],
    ["ee_frame", check.eeFrame],
  ];
  return (
    <div data-testid="marvel0-check-c" data-ok={String(check.ok)}>
      <p className={check.ok && check.fresh ? "rg-ok" : "rg-bad"}>
        {sayText(
          {
            key: check.ok
              ? "marvel0.ctl.check.c_passed"
              : "marvel0.ctl.check.c_failed",
            params: { ago: { ago: check.ageS } },
          },
          t,
        )}
        {check.ok && !check.fresh && <> {t("marvel0.ctl.check.too_old")}</>}
      </p>
      <ul className="rg-notes">
        {rows.map(([name, ok]) => (
          <li key={name} data-ok={String(ok)}>
            {wordOf(t, "marvel0.ctl.check.c_", name)}:{" "}
            {t(
              ok === true
                ? "marvel0.stack.check.pass"
                : ok === false
                  ? "marvel0.stack.check.fail"
                  : "marvel0.stack.check.unknown",
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function GripperLastLine({ last }: { last: GripperLast | null }) {
  const { t } = useLocale();
  if (!last) return <p className="rg-muted">{t("marvel0.ctl.gripper.none")}</p>;
  const what = last.action === "close" ? "close" : "open";
  const outcome = gripperOutcome(last);
  const age =
    last.at === null ? null : Math.max(0, Date.now() / 1000 - last.at);
  return (
    <p
      className={outcome === "reached" ? "rg-ok" : "rg-bad"}
      data-testid="marvel0-gripper-last"
      data-outcome={outcome}
      data-code={last.code ?? undefined}
    >
      {sayText(
        {
          key: `marvel0.ctl.gripper.last_${what}_${outcome}`,
          params: { ago: { ago: age } },
        },
        t,
      )}
      {outcome === "failed" && last.code && (
        <> {wordOf(t, "marvel0.ctl.gripper.code.", last.code)}</>
      )}
    </p>
  );
}

export function CompControls({
  comp,
  act,
  actions,
  lastOk,
  ask,
  next,
}: ControlProps) {
  const { t } = useLocale();
  const start = startControl(comp);
  const stop = stopControl(comp);
  const free =
    actions.working === null && !actions.locked && !operationRunning(act);
  const startKey = workKey({ kind: "start", comp: comp.id });
  const stopKey = workKey({ kind: "stop", comp: comp.id });
  const showStop = STOPPABLE.includes(comp.state) || stop.enabled;
  const refusedHere =
    actions.refused &&
    /^(start|stop|preflight):/.test(actions.refused.key) &&
    actions.refused.key.endsWith(`:${comp.id}`)
      ? actions.refused
      : null;
  return (
    <div
      className="rg-comp"
      data-testid={`marvel0-comp-${comp.id}`}
      data-state={comp.state}
      data-next={next || undefined}
    >
      {comp.reason && (
        <p
          className="rg-comp__reason"
          data-testid={`marvel0-reason-${comp.id}`}
        >
          <WordedText worded={comp.reason} />
        </p>
      )}
      {comp.failure && (comp.state === "failed" || comp.state === "dead") && (
        <p
          className="rg-bad"
          role="status"
          data-testid={`marvel0-failure-${comp.id}`}
        >
          <WordedText worded={comp.failure} />
        </p>
      )}
      {comp.readiness &&
        (comp.state === "starting" ||
          comp.state === "failed" ||
          comp.readiness.seen.length > 0) && (
          <Readiness comp={comp} lastOk={lastOk} />
        )}
      <div className="rg-comp__buttons">
        <Button
          variant="primary"
          icon={Play}
          size="md"
          loading={actions.working === startKey}
          disabled={!start.enabled || !free}
          data-next={next || undefined}
          onClick={() => ask({ kind: "start", comp: comp.id })}
        >
          {t("marvel0.ctl.btn.start")}
        </Button>
        {showStop && (
          <Button
            variant="secondary"
            icon={Square}
            size="md"
            className={
              comp.id in DANGEROUS_STOP ? "rg-btn--danger-outline" : undefined
            }
            loading={actions.working === stopKey}
            disabled={!stop.enabled || !free}
            onClick={() => ask({ kind: "stop", comp: comp.id })}
          >
            {t(
              comp.id === "A" || comp.id === "C"
                ? DANGEROUS_STOP[comp.id]
                : "marvel0.ctl.btn.stop",
            )}
          </Button>
        )}
        {comp.id === "A" && (
          <Button
            variant="secondary"
            icon={ShieldCheck}
            size="md"
            loading={
              actions.working === workKey({ kind: "preflight", comp: "A" })
            }
            disabled={!canCheckFci(act) || !free}
            onClick={() => void actions.check("A")}
          >
            {t("marvel0.ctl.btn.fci")}
          </Button>
        )}
        {comp.id === "C" && (
          <Button
            variant="secondary"
            icon={ShieldCheck}
            size="md"
            loading={
              actions.working === workKey({ kind: "preflight", comp: "C" })
            }
            disabled={!canCheckServer(act) || !free}
            onClick={() => void actions.check("C")}
          >
            {t("marvel0.ctl.btn.preflight_c")}
          </Button>
        )}
      </div>
      <Reasons
        label="marvel0.ctl.off.start"
        control={start}
        quiet={SELF_EXPLAINING.includes(comp.state)}
        testId={`marvel0-block-start-${comp.id}`}
      />
      {showStop && (
        <Reasons
          label="marvel0.ctl.off.stop"
          control={stop}
          testId={`marvel0-block-stop-${comp.id}`}
        />
      )}
      {actions.working === startKey && (
        <p className="rg-working" role="status" data-testid="marvel0-working">
          {t("marvel0.ctl.working.start")}
        </p>
      )}
      {actions.working === stopKey && (
        <p className="rg-working" role="status" data-testid="marvel0-working">
          {t("marvel0.ctl.working.stop")}
        </p>
      )}
      {refusedHere && (
        <RefusedBlock testId="marvel0-refused" code={refusedHere.failure.code}>
          <WordedText worded={refusedHere.worded} />
        </RefusedBlock>
      )}
      {comp.stopNote && comp.state !== "ready" && comp.state !== "starting" && (
        <p
          className="rg-warn"
          role="status"
          data-testid={`marvel0-stopnote-${comp.id}`}
        >
          {wordOf(t, "marvel0.ctl.stop_note.", comp.stopNote)}
        </p>
      )}
      {comp.id === "A" && <FciLine act={act} />}
      {comp.id === "C" && <ServerLine act={act} />}
      <p className="rg-muted rg-comp__hazard">
        {wordOf(t, "marvel0.ctl.hazard.card_", comp.id)}
      </p>
    </div>
  );
}

/** One explicit open/close click acknowledges the visible hands-clear notice.
 * Backend gates, request identity, busy locking and the last result are shared. */
export function GripperTest({
  act,
  actions,
}: {
  act: ActView;
  actions: StackActions;
}) {
  const { t } = useLocale();
  const g = act.gripper;
  const control: Control = {
    enabled: g.canRun,
    blockers: g.blockers,
    needsOverride: false,
  };
  const off =
    !g.canRun || g.running || actions.working !== null || actions.locked;
  const refused = actions.refused?.key.startsWith("gripper:")
    ? actions.refused
    : null;
  return (
    <section
      className="rg-gripper"
      aria-labelledby="marvel0-gripper-title"
      data-testid="marvel0-gripper-test"
    >
      <h4 id="marvel0-gripper-title" className="rg-gripper__title">
        {t("marvel0.ctl.gripper.title")}
      </h4>
      <p className="rg-muted" id="marvel0-gripper-click-notice">
        {t("marvel0.ctl.gripper.single_click")}
      </p>
      <div className="rg-comp__buttons">
        <Button
          icon={MoveHorizontal}
          loading={actions.working === "gripper:open"}
          disabled={off}
          aria-describedby="marvel0-gripper-click-notice"
          onClick={() => {
            if (!off)
              void actions.gripper("open", phraseFor(act, "gripper_open"));
          }}
        >
          {t("marvel0.ctl.btn.gripper_open")}
        </Button>
        <Button
          icon={Hand}
          loading={actions.working === "gripper:close"}
          disabled={off}
          aria-describedby="marvel0-gripper-click-notice"
          onClick={() => {
            if (!off)
              void actions.gripper("close", phraseFor(act, "gripper_close"));
          }}
        >
          {t("marvel0.ctl.btn.gripper_close")}
        </Button>
      </div>
      {g.running && (
        <p
          className="rg-working"
          role="status"
          data-testid="marvel0-gripper-running"
        >
          {t("marvel0.ctl.gripper.running")}
        </p>
      )}
      <Reasons
        label="marvel0.ctl.off.gripper"
        control={control}
        testId="marvel0-block-gripper"
      />
      {refused && (
        <RefusedBlock code={refused.failure.code}>
          <WordedText worded={refused.worded} />
        </RefusedBlock>
      )}
      <GripperLastLine last={g.last} />
    </section>
  );
}
