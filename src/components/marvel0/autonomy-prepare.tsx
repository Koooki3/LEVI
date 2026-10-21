"use client";
// The part of Step 5 that comes before terminal D: the passive readiness rows,
// the form that prepares a run (task, mode, rounds, steps, the gripper wait,
// whether D homes first), the prepared run with the command the terminal
// fallback uses, and "Start terminal D". Nothing here moves the arm: a prepared
// run is two files and two digests written under the runs folder, and the start
// launches D only up to the confirmation prompt, which is a later key on the
// run card. The two actions that stop something (the live annotation service on
// :8100, leftover AERI units) are separate buttons with their own windows.
//
// Readiness is shown, not decided here: a row is ok, a warning, a refusal or
// unknown, and an unknown refuses like a refusal. The backend checks every row
// again at the start.
import { useEffect, useRef, useState } from "react";
import {
  Check,
  CircleHelp,
  CircleX,
  ClipboardCopy,
  Lock,
  Play,
  TriangleAlert,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  SegmentedControl,
  Switch,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { RefusedBlock, WordedText } from "./action-parts";
import { ConfirmRefusal, ConfirmWindow } from "./confirm-window";
import { wordOf } from "./ctl-text";
import { LiveAge } from "./live-age";
import { Notice } from "./notice";
import { sayText } from "./marvel0-say";
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import {
  MODES,
  type PreparedView,
  type ReadyRow,
  type ReadyView,
  type RunView,
} from "./autonomy-doc";
import {
  DEFAULT_LIMITS,
  READY_FRESH_MS,
  blockingRows,
  initialForm,
  levelKey,
  modeHintKey,
  modeKey,
  prepareErrors,
  rangeOf,
  shortSha,
  startOff,
  type PrepareField,
  type PrepareForm,
} from "./autonomy-logic";
import type { AutonomyActions } from "./use-autonomy";
import type { Polled } from "./use-polled";

const LEVEL_ICON = {
  ok: Check,
  warn: TriangleAlert,
  refuse: CircleX,
  unknown: CircleHelp,
} as const;

function ReadyItem({ row }: { row: ReadyRow }) {
  const { t } = useLocale();
  const Glyph = LEVEL_ICON[row.level];
  return (
    <li data-level={row.level} data-code={row.code}>
      <Glyph size={14} aria-hidden="true" />
      <span>
        <span className="rg-aready__level">{t(levelKey(row.level))}</span>{" "}
        <WordedText worded={row.worded} />
      </span>
    </li>
  );
}

/** The readiness rows: what refuses (and what is unknown, which refuses too)
 * first, then warnings, then what is met, folded. */
export function ReadinessList({ ready }: { ready: ReadyView }) {
  const { t } = useLocale();
  const refusing = blockingRows(ready.rows);
  const warns = ready.rows.filter((row) => row.level === "warn");
  const met = ready.rows.filter((row) => row.level === "ok");
  return (
    <section
      className="rg-aready"
      data-testid="marvel0-autonomy-ready"
      aria-labelledby="marvel0-autonomy-ready-title"
    >
      <div className="rg-actions-row">
        <h4 id="marvel0-autonomy-ready-title" className="rg-marker__title">
          {t("marvel0.autonomy.ready.title")}
        </h4>
        <span data-testid="marvel0-autonomy-ready-count">
          <Badge tone={refusing.length === 0 ? "success" : "danger"}>
            {sayText(
              {
                key: "marvel0.autonomy.ready.count",
                params: { n: met.length, total: ready.rows.length },
              },
              t,
            )}
          </Badge>
        </span>
      </div>
      {ready.rows.length === 0 && (
        <p className="rg-muted">{t("marvel0.autonomy.ready.empty")}</p>
      )}
      {refusing.length > 0 && (
        <ul
          className="rg-aready__list"
          data-testid="marvel0-autonomy-ready-refusing"
        >
          {refusing.map((row) => (
            <ReadyItem key={row.code} row={row} />
          ))}
        </ul>
      )}
      {warns.length > 0 && (
        <ul className="rg-aready__list">
          {warns.map((row) => (
            <ReadyItem key={row.code} row={row} />
          ))}
        </ul>
      )}
      {met.length > 0 && (
        <details className="rg-details">
          <summary>
            {sayText(
              { key: "marvel0.autonomy.ready.met", params: { n: met.length } },
              t,
            )}
          </summary>
          <ul className="rg-aready__list">
            {met.map((row) => (
              <ReadyItem key={row.code} row={row} />
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function CopyCommand({ command }: { command: string }) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const text = useRef<HTMLPreElement>(null);
  // Without the clipboard (a page that is not a secure context) the command is
  // selected, so that Ctrl+C copies it.
  const select = () => {
    try {
      if (text.current) window.getSelection()?.selectAllChildren(text.current);
    } catch {
      // nothing to select in
    }
  };
  const copy = () => {
    const write = navigator.clipboard?.writeText(command);
    if (!write) return select();
    write
      .then(() => {
        setCopied(true);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 2000);
      })
      .catch(select);
  };
  return (
    <div className="rg-aprep__cmd">
      <div className="rg-actions-row">
        <span className="rg-muted">
          {t("marvel0.autonomy.prepared.command")}
        </span>
        <Button size="sm" icon={copied ? Check : ClipboardCopy} onClick={copy}>
          {copied ? t("Copied") : t("Copy")}
        </Button>
      </div>
      <pre
        ref={text}
        lang="en"
        className="rg-command"
        data-testid="marvel0-autonomy-command"
        tabIndex={0}
      >
        {command}
      </pre>
    </div>
  );
}

const START_ACKS = ["estop", "scene_ok", "area_clear", "arm_home"] as const;

function PreparedCard({
  prepared,
  ready,
  actions,
  off,
}: {
  prepared: PreparedView;
  ready: ReadyView;
  actions: AutonomyActions;
  off: ReturnType<typeof startOff>;
}) {
  const { t } = useLocale();
  const [asking, setAsking] = useState(false);
  const refused = actions.refused?.key === "start" ? actions.refused : null;
  const rows: Array<[string, string | null]> = [
    [t("marvel0.autonomy.prepared.name"), prepared.name],
    [
      t("marvel0.autonomy.prepared.mode"),
      prepared.mode ? t(modeKey(prepared.mode)) : null,
    ],
    [t("marvel0.autonomy.prepared.task"), prepared.task],
    [
      t("marvel0.autonomy.prepared.rounds"),
      prepared.rounds === null ? null : String(prepared.rounds),
    ],
    [
      t("marvel0.autonomy.prepared.steps"),
      prepared.steps === null ? null : String(prepared.steps),
    ],
    [
      t("marvel0.autonomy.prepared.gripper"),
      prepared.gripperConfirmS === null
        ? null
        : `${prepared.gripperConfirmS} s`,
    ],
    [
      t("marvel0.autonomy.prepared.home"),
      t(prepared.startHome ? "marvel0.autonomy.yes" : "marvel0.autonomy.no"),
    ],
    [t("marvel0.autonomy.prepared.plan"), shortSha(prepared.planSha)],
    [t("marvel0.autonomy.prepared.binding"), shortSha(prepared.bindingSha)],
  ];
  const working = actions.working !== null;
  return (
    <section
      className="rg-aprep"
      data-testid="marvel0-autonomy-prepared"
      aria-labelledby="marvel0-autonomy-prepared-title"
    >
      <h4 id="marvel0-autonomy-prepared-title" className="rg-marker__title">
        {t("marvel0.autonomy.prepared.title")}
      </h4>
      <dl className="rg-facts">
        {rows
          .filter((row): row is [string, string] => !!row[1])
          .map(([label, value]) => (
            <div className="rg-fact" key={label}>
              <dt>{label}</dt>
              <dd>
                <code lang="en">{value}</code>
              </dd>
            </div>
          ))}
      </dl>
      {!prepared.intact && (
        <Notice
          tone="warning"
          role="status"
          testId="marvel0-autonomy-prepared-changed"
        >
          {t("marvel0.autonomy.prepared.changed")}{" "}
          {prepared.problems.map((code) => (
            <code key={code} lang="en">
              {code}{" "}
            </code>
          ))}
        </Notice>
      )}
      {prepared.execute && <CopyCommand command={prepared.execute} />}
      <p className="rg-muted">{t("marvel0.autonomy.prepared.fallback")}</p>
      <div className="rg-runstart">
        <Button
          variant="primary"
          icon={Play}
          disabled={off.length > 0 || working}
          loading={actions.working === "start"}
          onClick={() => {
            actions.clear();
            setAsking(true);
          }}
          data-testid="marvel0-autonomy-start"
        >
          {t("marvel0.autonomy.start.button")}
        </Button>
      </div>
      {off.length > 0 && (
        <div className="rg-block" data-testid="marvel0-autonomy-start-off">
          <span className="rg-block__label">
            <Lock size={12} aria-hidden="true" />
            {t("marvel0.autonomy.start.off")}
          </span>
          <ul className="rg-block__list">
            {off.map((row) => (
              <li key={row.code} data-code={row.code}>
                <WordedText worded={row.worded} />
              </li>
            ))}
          </ul>
        </div>
      )}
      <ConfirmWindow
        open={asking}
        testId="marvel0-autonomy-start-dialog"
        title={t("marvel0.autonomy.start.title")}
        intro={t("marvel0.autonomy.start.intro")}
        facts={
          <dl className="rg-facts">
            <div className="rg-fact">
              <dt>{t("marvel0.autonomy.prepared.name")}</dt>
              <dd>
                <code lang="en">{prepared.name}</code>
              </dd>
            </div>
            {ready.profile && (
              <div className="rg-fact">
                <dt>{t("marvel0.autonomy.profile.label")}</dt>
                <dd>
                  <code lang="en">{ready.profile.id}</code>
                </dd>
              </div>
            )}
          </dl>
        }
        estop={t("marvel0.autonomy.estop_line")}
        hazards={[t("marvel0.autonomy.start.hazard")]}
        notes={[t("marvel0.autonomy.gates_scope")]}
        attest={START_ACKS.map((code) => ({
          code,
          label: wordOf(t, "marvel0.autonomy.ack.", code),
        }))}
        confirmLabel={t("marvel0.autonomy.start.go")}
        confirmIcon={Play}
        ready={!working && off.length === 0}
        working={actions.working === "start"}
        refused={
          refused ? (
            <ConfirmRefusal
              failure={refused.failure}
              testId="marvel0-autonomy-start-refused"
            >
              <WordedText worded={refused.worded} />
            </ConfirmRefusal>
          ) : null
        }
        onCancel={() => setAsking(false)}
        onConfirm={(ticked) =>
          actions
            .start(prepared.name, {
              estop: ticked.estop === true,
              scene_ok: ticked.scene_ok === true,
              area_clear: ticked.area_clear === true,
              arm_home: ticked.arm_home === true,
            })
            .then((ok) => {
              if (ok) setAsking(false);
            })
        }
      />
    </section>
  );
}

function PrepareFormCard({
  ready,
  actions,
}: {
  ready: ReadyView;
  actions: AutonomyActions;
}) {
  const { t } = useLocale();
  const [edited, setEdited] = useState<PrepareForm | null>(null);
  const [touched, setTouched] = useState(false);
  const form = edited ?? initialForm(ready);
  const limits = ready.limits ?? DEFAULT_LIMITS;
  const errors = prepareErrors(form, limits);
  const set = (change: Partial<PrepareForm>) => {
    setEdited({ ...form, ...change });
    setTouched(true);
  };
  const refused = actions.refused?.key === "prepare" ? actions.refused : null;
  const homeAble = ready.profile?.startHome === true;
  // The hint of a field: what this page sees wrong with it, else what the
  // backend refused it for (`form_invalid` names the field and the reason).
  const refusedField =
    refused?.failure.code === "form_invalid"
      ? (refused.failure.field ??
        (typeof refused.failure.extra.field === "string"
          ? refused.failure.extra.field
          : null))
      : null;
  const refusedReason =
    typeof refused?.failure.extra.reason === "string"
      ? refused.failure.extra.reason
      : "range";
  const shown = (field: PrepareField) => {
    if (touched && errors[field]) {
      const range = rangeOf(field, limits);
      return range
        ? sayText(
            {
              key: errors[field]!,
              params: { min: range.min, max: range.max },
            },
            t,
          )
        : t(errors[field]!);
    }
    return refusedField === field
      ? wordOf(t, "marvel0.autonomy.form.refused_", refusedReason)
      : undefined;
  };
  return (
    <form
      className="rg-aform"
      data-testid="marvel0-autonomy-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (Object.keys(errors).length === 0 && actions.working === null)
          void actions.prepare(form);
      }}
    >
      <h4 className="rg-marker__title">{t("marvel0.autonomy.form.title")}</h4>
      <div className="rg-form--grid">
        <Field
          className="rg-span"
          label={t("marvel0.autonomy.form.task")}
          hint={t("marvel0.autonomy.form.task_hint")}
          error={shown("task")}
        >
          <Input
            value={form.task}
            autoComplete="off"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            lang="en"
            placeholder={t("marvel0.autonomy.form.task_example")}
            onChange={(event) => set({ task: event.target.value })}
          />
        </Field>
        <div className="rg-span">
          <SegmentedControl
            label={t("marvel0.autonomy.form.mode")}
            value={form.mode}
            options={MODES.map((mode) => ({
              value: mode,
              label: t(modeKey(mode)),
            }))}
            onChange={(value) => set({ mode: value as PrepareForm["mode"] })}
          />
          <p className="rg-muted" data-testid="marvel0-autonomy-mode-hint">
            {t(modeHintKey(form.mode))}
          </p>
        </div>
        <div className="rg-span rg-aform__nums">
          <Field
            label={t("marvel0.autonomy.form.rounds")}
            hint={sayText(
              {
                key: "marvel0.autonomy.form.rounds_hint",
                params: { min: limits.rounds.min, max: limits.rounds.max },
              },
              t,
            )}
            error={shown("rounds")}
          >
            <Input
              value={form.rounds}
              inputMode="numeric"
              autoComplete="off"
              onChange={(event) => set({ rounds: event.target.value })}
            />
          </Field>
          <Field
            label={t("marvel0.autonomy.form.steps")}
            hint={sayText(
              {
                key: "marvel0.autonomy.form.steps_hint",
                params: { min: limits.steps.min, max: limits.steps.max },
              },
              t,
            )}
            error={shown("steps")}
          >
            <Input
              value={form.steps}
              inputMode="numeric"
              autoComplete="off"
              onChange={(event) => set({ steps: event.target.value })}
            />
          </Field>
          <Field
            label={t("marvel0.autonomy.form.gripper")}
            hint={sayText(
              {
                key: "marvel0.autonomy.form.gripper_hint",
                params: { min: limits.gripper.min, max: limits.gripper.max },
              },
              t,
            )}
            error={shown("gripper_confirm_s")}
          >
            <Input
              value={form.gripperConfirmS}
              inputMode="decimal"
              autoComplete="off"
              onChange={(event) => set({ gripperConfirmS: event.target.value })}
            />
          </Field>
        </div>
        <div className="rg-span">
          <Switch
            checked={homeAble && form.startHome}
            disabled={!homeAble}
            onChange={(event) => set({ startHome: event.target.checked })}
            label={t("marvel0.autonomy.form.start_home")}
          />
          <p className="rg-muted">
            {t(
              homeAble
                ? "marvel0.autonomy.form.start_home_hint"
                : "marvel0.autonomy.form.start_home_none",
            )}
          </p>
        </div>
      </div>
      <div className="rg-runstart">
        <Button
          type="submit"
          variant="secondary"
          disabled={actions.working !== null || Object.keys(errors).length > 0}
          loading={actions.working === "prepare"}
          data-testid="marvel0-autonomy-prepare"
        >
          {t("marvel0.autonomy.form.submit")}
        </Button>
        <span className="rg-muted">
          {t("marvel0.autonomy.form.submit_hint")}
        </span>
      </div>
      {refused && (
        <RefusedBlock
          testId="marvel0-autonomy-prepare-refused"
          code={refused.failure.code}
        >
          <WordedText worded={refused.worded} />
        </RefusedBlock>
      )}
    </form>
  );
}

/** A button with a confirm window that stops something the card names: the
 * live annotation service (it holds :8100) or the leftover AERI units. */
function StopUnits({
  testId,
  actionKey,
  titleKey,
  introKey,
  hazardKey,
  buttonKey,
  run,
  actions,
}: {
  testId: string;
  actionKey: "live_stop" | "leftovers_stop";
  titleKey: string;
  introKey: string;
  hazardKey: string;
  buttonKey: string;
  run: () => Promise<boolean>;
  actions: AutonomyActions;
}) {
  const { t } = useLocale();
  const [asking, setAsking] = useState(false);
  const refused = actions.refused?.key === actionKey ? actions.refused : null;
  return (
    <div className="rg-astopunits" data-testid={testId}>
      <p>{t(introKey)}</p>
      <Button
        variant="secondary"
        disabled={actions.working !== null}
        onClick={() => {
          actions.clear();
          setAsking(true);
        }}
      >
        {t(buttonKey)}
      </Button>
      <ConfirmWindow
        open={asking}
        testId={`${testId}-dialog`}
        title={t(titleKey)}
        intro={t(introKey)}
        hazards={[t(hazardKey)]}
        notes={[t("marvel0.autonomy.gates_scope")]}
        confirmLabel={t(buttonKey)}
        ready={actions.working === null}
        working={actions.working === actionKey}
        refused={
          refused ? (
            <ConfirmRefusal
              failure={refused.failure}
              testId={`${testId}-refused`}
            >
              <WordedText worded={refused.worded} />
            </ConfirmRefusal>
          ) : null
        }
        onCancel={() => setAsking(false)}
        onConfirm={() =>
          run().then((ok) => {
            if (ok) setAsking(false);
          })
        }
      />
    </div>
  );
}

/** Something is in the way (live service, leftover units) that this page may
 * not stop now; the backend says why. */
function CannotStop({
  testId,
  introKey,
  why,
}: {
  testId: string;
  introKey: string;
  why: Worded<Say> | null;
}) {
  const { t } = useLocale();
  return (
    <div className="rg-astopunits" data-testid={testId}>
      <p>{t(introKey)}</p>
      <p className="rg-block">
        <span className="rg-block__label">
          <Lock size={12} aria-hidden="true" />
          {t("marvel0.autonomy.cannot_stop")}
        </span>{" "}
        {why ? (
          <WordedText worded={why} />
        ) : (
          t("marvel0.autonomy.cannot_stop_unknown")
        )}
      </p>
    </div>
  );
}

export function PrepareCard({
  poll,
  actions,
  evalLive,
  runLive,
  run,
}: {
  poll: Polled<ReadyView>;
  actions: AutonomyActions;
  /** The hosted D's document: its `start` block is part of the answer to
   * "may a start be clicked". */
  run: RunView | null;
  /** The hosted evaluation client is live: only one session at a time. */
  evalLive: boolean;
  /** A hosted D is live. */
  runLive: boolean;
}) {
  const { t } = useLocale();
  const ready = poll.value;
  const off = startOff(ready, { evalLive, runLive, stale: !poll.current, run });
  return (
    <Card
      padding="compact"
      tabIndex={-1}
      className="rg-card rg-aprepare rg-card--not_started"
      data-testid="marvel0-autonomy-prepare-card"
      title={t("marvel0.autonomy.prepare.title")}
      description={t("marvel0.autonomy.prepare.scope")}
      actions={
        <span className="rg-run__meta">
          <LiveAge
            lastOk={poll.lastOk}
            warnAfterMs={READY_FRESH_MS / 2}
            testId="marvel0-autonomy-ready-age"
          />
          {ready?.profile && (
            <span data-testid="marvel0-autonomy-ready-profile">
              <Badge tone={ready.profile.known ? "neutral" : "danger"}>
                {sayText(
                  {
                    key: "marvel0.autonomy.profile.badge",
                    params: { id: ready.profile.id },
                  },
                  t,
                )}
              </Badge>
            </span>
          )}
        </span>
      }
    >
      {!ready && poll.loaded && (
        <Notice
          tone="warning"
          role="status"
          testId="marvel0-autonomy-ready-unreadable"
        >
          {t("marvel0.autonomy.ready.unreadable")}
        </Notice>
      )}
      {ready && !poll.current && (
        <Notice
          tone="warning"
          role="status"
          testId="marvel0-autonomy-ready-stale"
        >
          {t("marvel0.autonomy.ready.stale")}
        </Notice>
      )}
      {ready && ready.enabled && ready.site.state === "ok" && (
        <>
          <ReadinessList ready={ready} />
          {ready.liveService?.state === "running" &&
            (ready.liveService.canStop ? (
              <StopUnits
                testId="marvel0-autonomy-live"
                actionKey="live_stop"
                titleKey="marvel0.autonomy.live.title"
                introKey="marvel0.autonomy.live.intro"
                hazardKey="marvel0.autonomy.live.hazard"
                buttonKey="marvel0.autonomy.live.button"
                run={actions.stopLive}
                actions={actions}
              />
            ) : (
              <CannotStop
                testId="marvel0-autonomy-live-blocked"
                introKey="marvel0.autonomy.live.intro"
                why={ready.liveService.whyNot}
              />
            ))}
          {ready.leftovers?.state === "present" &&
            (ready.leftovers.canStop ? (
              <StopUnits
                testId="marvel0-autonomy-leftovers"
                actionKey="leftovers_stop"
                titleKey="marvel0.autonomy.leftovers.title"
                introKey="marvel0.autonomy.leftovers.intro"
                hazardKey="marvel0.autonomy.leftovers.hazard"
                buttonKey="marvel0.autonomy.leftovers.button"
                run={actions.stopLeftovers}
                actions={actions}
              />
            ) : (
              <CannotStop
                testId="marvel0-autonomy-leftovers-blocked"
                introKey="marvel0.autonomy.leftovers.intro"
                why={ready.leftovers.whyNot}
              />
            ))}
          {ready.prepared && (
            <PreparedCard
              prepared={ready.prepared}
              ready={ready}
              actions={actions}
              off={off}
            />
          )}
          {ready.prepared ? (
            // A run is prepared: the next thing to do is start it. The form
            // for another one is there, folded.
            <details
              className="rg-details rg-aanother"
              data-testid="marvel0-autonomy-another"
            >
              <summary>{t("marvel0.autonomy.prepare.another")}</summary>
              <PrepareFormCard ready={ready} actions={actions} />
            </details>
          ) : (
            <PrepareFormCard ready={ready} actions={actions} />
          )}
        </>
      )}
    </Card>
  );
}
