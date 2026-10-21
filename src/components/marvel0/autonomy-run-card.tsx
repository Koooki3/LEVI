"use client";
// The card of the terminal D this page hosts (Step 5), top to bottom: its
// state and how old that is; Stop (a full-width block that is there while D
// lives and does not wait for a fresh read); what D waits for, quoting its own
// screen line; how far the rounds are, from D's journal; the objects the vision
// model listed; what a WAIT needs from a person; the keys offered now; how the
// run ends when its wrap-up is stuck; the agent's queue (external mode); D's
// screen, the clicks made here, notices; after the exit, what is left over and
// the way to the run's own page.
//
// Honest wording is part of the card, not a footnote: a click here stands for
// the same key in terminal D, nothing on the page is an emergency stop, Stop is
// Ctrl+C (the arm stays where it is and does not go home), and the gates stop
// mistakes, not another program of the same account (autonomy-step.tsx puts
// that sentence above both cards, always shown). The labels s, f and d are the
// person's: the page never labels, and the way to the run's own page (which
// shows the vision model's judgements) is given only after the run ended.
import { useMemo, useState } from "react";
import { CircleStop, ExternalLink, RefreshCw, Square } from "lucide-react";
import { Badge, Button, Card, Progress } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import { RefusedBlock, WordedText } from "./action-parts";
import { ConfirmRefusal, ConfirmWindow } from "./confirm-window";
import { wordOf } from "./ctl-text";
import { LiveAge } from "./live-age";
import { Notice } from "./notice";
import { sayText } from "./marvel0-say";
import { KeyConfirm, KeysBlock, useKeyPress } from "./autonomy-keys";
import { KEY_IDS, type KeyId, type RunView } from "./autonomy-doc";
import {
  LIMITS,
  RUN_FRESH_MS,
  STATE_TONE,
  attestOf,
  exitKind,
  hasRun,
  isLive,
  keyChecks,
  keyVM,
  mayMove,
  modeKey,
  promptLine,
  runPageHref,
  stateHintKey,
  stateKey,
  stopControl,
  stopUnacknowledged,
  stopWhyKey,
  visibleKeys,
  type KeyCtx,
  type Limits,
  type StopMode,
} from "./autonomy-logic";
import {
  useArming,
  useClock,
  type AutonomyActions,
  type AutonomyRunPoll,
} from "./use-autonomy";

const STOP_CAPTION: Record<string, string> = {
  ready: "marvel0.autonomy.stop.ready",
  sent: "marvel0.autonomy.stop.sent",
  acked: "marvel0.autonomy.stop.acked",
  retry: "marvel0.autonomy.stop.retry",
  spent: "marvel0.autonomy.stop.spent",
};

/** Why no Stop is offered, for a Stop frame that is off (`off`: none was sent;
 * `unoffered`: one was, D did not answer, and no more is offered). The reason is
 * the document's own code: the words of the three codes the Stop frame has
 * its own sentences for, else the document's wording of it (a D locked after a
 * fault is "Not offered while D is fault_locked"). It is read from the last good
 * read, however old. */
function StopReason({ poll }: { poll: AutonomyRunPoll }) {
  const { t } = useLocale();
  const key = stopWhyKey(poll.lastSeen?.whyCode ?? null);
  if (key) return <>{t(key)}</>;
  const worded = poll.value?.stop.whyNot ?? null;
  return worded ? <WordedText worded={worded} /> : null;
}

/** The sentence under the Stop button, from the mode and the reason together
 * (review N2: from the phase alone it said "one more try is offered" next to a
 * button that was off). */
export function StopCaption({
  mode,
  poll,
  short = false,
}: {
  mode: StopMode;
  poll: AutonomyRunPoll;
  /** Without the way out that needs no page (the dock is small on a phone; the
   * card holds the whole sentence). */
  short?: boolean;
}) {
  const { t } = useLocale();
  if (mode === "off")
    return (
      <>
        {t("marvel0.autonomy.stop.off")} <StopReason poll={poll} />
      </>
    );
  if (mode === "unoffered")
    return (
      <>
        {t("marvel0.autonomy.stop.unoffered")} <StopReason poll={poll} />
        {!short && ` ${t("marvel0.autonomy.stop.unoffered_tail")}`}
      </>
    );
  return <>{t(STOP_CAPTION[mode])}</>;
}

/** Stop: Ctrl+C for the hosted D. A block of its own, full width and 64 px
 * high, in a place that does not move; on while D lives, by the last good read
 * and however old it is. Not an emergency stop, and the caption says so. */
export function StopFrame({
  poll,
  actions,
}: {
  poll: AutonomyRunPoll;
  actions: AutonomyActions;
}) {
  const { t } = useLocale();
  const { mode, enabled } = stopControl(poll.lastSeen, actions.stopSent);
  if (mode === "hidden") return null;
  const refused = actions.refused?.key === "stop" ? actions.refused : null;
  return (
    <div
      className="rg-astop"
      data-testid="marvel0-autonomy-stop-frame"
      data-mode={mode}
    >
      <Button
        variant="danger"
        size="lg"
        className="rg-astop__btn"
        icon={Square}
        loading={actions.stopping}
        disabled={!enabled}
        onClick={() => void actions.stop()}
        data-testid="marvel0-autonomy-stop"
      >
        {t("marvel0.autonomy.stop.button")}
      </Button>
      <p
        className="rg-hold__caption"
        role="status"
        data-testid="marvel0-autonomy-stop-caption"
      >
        {actions.stopping ? (
          t("marvel0.autonomy.stop.sending")
        ) : (
          <StopCaption mode={mode} poll={poll} />
        )}
      </p>
      {refused && (
        <RefusedBlock
          testId="marvel0-autonomy-stop-refused"
          code={refused.failure.code}
        >
          <WordedText worded={refused.worded} />
        </RefusedBlock>
      )}
    </div>
  );
}

function ProgressBlock({ run }: { run: RunView }) {
  const { t } = useLocale();
  const p = run.progress;
  if (!p || (p.rounds === null && p.round === null) || p.rounds === 0)
    return null;
  const max = p.rounds ?? Math.max(p.round ?? 0, 1);
  return (
    <div className="rg-aprogress" data-testid="marvel0-autonomy-progress">
      <Progress
        value={Math.min(p.released ?? 0, max)}
        max={max}
        label={sayText(
          {
            key:
              p.round === null
                ? "marvel0.autonomy.progress.none"
                : "marvel0.autonomy.progress.round",
            params: { round: p.round ?? 0, rounds: p.rounds ?? "?" },
          },
          t,
        )}
      />
      {p.step !== null && p.maxSteps !== null && p.maxSteps > 0 && (
        <Progress
          value={Math.min(p.step, p.maxSteps)}
          max={p.maxSteps}
          label={sayText(
            {
              key: "marvel0.autonomy.progress.step",
              params: { step: p.step, max: p.maxSteps },
            },
            t,
          )}
        />
      )}
      <p className="rg-muted rg-aprogress__facts">
        {sayText(
          {
            key: "marvel0.autonomy.progress.facts",
            params: { released: p.released ?? 0, discarded: p.discarded ?? 0 },
          },
          t,
        )}
        {p.verdictLatched && ` ${t("marvel0.autonomy.progress.verdict_used")}`}
        {(p.discarded ?? 0) > 0 &&
          ` ${t("marvel0.autonomy.progress.discard_adds")}`}
      </p>
    </div>
  );
}

function Inventory({ run }: { run: RunView }) {
  const { t } = useLocale();
  const inv = run.inventory;
  if (!inv) return null;
  return (
    <section
      className="rg-ainv"
      data-testid="marvel0-autonomy-inventory"
      aria-labelledby="marvel0-autonomy-inv-title"
    >
      <div className="rg-actions-row">
        <h4 id="marvel0-autonomy-inv-title" className="rg-marker__title">
          {t("marvel0.autonomy.inv.title")}
        </h4>
        {inv.confirmed && (
          <span data-testid="marvel0-autonomy-inv-confirmed">
            <Badge tone="success">{t("marvel0.autonomy.inv.confirmed")}</Badge>
          </span>
        )}
      </div>
      {inv.items.length === 0 ? (
        <p className="rg-muted">{t("marvel0.autonomy.inv.empty")}</p>
      ) : (
        <table className="rg-atable">
          <thead>
            <tr>
              <th scope="col">{t("marvel0.autonomy.inv.id")}</th>
              <th scope="col">{t("marvel0.autonomy.inv.colour")}</th>
              <th scope="col">{t("marvel0.autonomy.inv.kind")}</th>
              <th scope="col">{t("marvel0.autonomy.inv.on")}</th>
              <th scope="col">{t("marvel0.autonomy.inv.role")}</th>
            </tr>
          </thead>
          <tbody>
            {inv.items.map((item) => (
              <tr key={item.id} data-testid="marvel0-autonomy-inv-row">
                <td data-label={t("marvel0.autonomy.inv.id")}>
                  <code lang="en">{item.id}</code>
                </td>
                <td data-label={t("marvel0.autonomy.inv.colour")} lang="en">
                  {item.colour ?? "—"}
                </td>
                <td data-label={t("marvel0.autonomy.inv.kind")} lang="en">
                  {item.kind ?? "—"}
                </td>
                <td data-label={t("marvel0.autonomy.inv.on")}>
                  <code lang="en">{item.on ?? "—"}</code>
                </td>
                <td data-label={t("marvel0.autonomy.inv.role")}>
                  {item.graspable === null
                    ? "—"
                    : t(
                        item.graspable
                          ? "marvel0.autonomy.inv.graspable"
                          : "marvel0.autonomy.inv.target",
                      )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function WaitBlock({ run }: { run: RunView }) {
  const { t } = useLocale();
  const wait = run.wait;
  if (!wait) return null;
  const words = [wait.text, wait.todo].filter(Boolean).join(" ");
  return (
    <div className="rg-await" data-testid="marvel0-autonomy-wait">
      {wait.reason && (
        <p>
          <strong>{t("marvel0.autonomy.wait_reason")}</strong>{" "}
          {wordOf(t, "marvel0.autonomy.waitreason.", wait.reason)}
        </p>
      )}
      {words !== "" && (
        // D's own words for what it waits for, in its console language.
        <blockquote
          className="rg-aprompt"
          data-testid="marvel0-autonomy-wait-text"
          lang={run.profile?.consoleLang ?? undefined}
        >
          {words}
        </blockquote>
      )}
      {wait.confirm && (
        <p className="rg-muted" data-testid="marvel0-autonomy-wait-confirm">
          {t("marvel0.autonomy.wait_confirm")}
        </p>
      )}
      {wait.restoreScene && (
        // Before the key that asks to resume: the scene and the arm come first.
        <Notice tone="warning" role="status" testId="marvel0-autonomy-restore">
          {t("marvel0.autonomy.restore_scene")}
        </Notice>
      )}
      {wait.recent.length > 0 && (
        <ul className="rg-notes" lang="en">
          {wait.recent.map((row, index) => (
            <li key={`${row.code}:${index}`}>
              <code>{row.code}</code>
              {row.detail ? `: ${row.detail}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function EndForced({
  run,
  actions,
}: {
  run: RunView;
  actions: AutonomyActions;
}) {
  const { t } = useLocale();
  const [asking, setAsking] = useState(false);
  const refused = actions.refused?.key === "end" ? actions.refused : null;
  if (!run.d.alive) return null;
  return (
    <>
      <details
        className="rg-details rg-aend"
        data-testid="marvel0-autonomy-end"
        open={run.end.allowed || undefined}
      >
        <summary>{t("marvel0.autonomy.end.summary")}</summary>
        <p className="rg-muted">{t("marvel0.autonomy.end.hint")}</p>
        <Button
          variant="secondary"
          className="rg-btn--danger-outline"
          icon={CircleStop}
          disabled={!run.end.allowed || actions.working !== null}
          onClick={() => {
            actions.clear();
            setAsking(true);
          }}
        >
          {t("marvel0.autonomy.end.button")}
        </Button>
        {!run.end.allowed && run.end.whyNot && (
          <p className="rg-block" data-testid="marvel0-autonomy-end-why">
            <WordedText worded={run.end.whyNot} />
          </p>
        )}
      </details>
      <ConfirmWindow
        open={asking}
        testId="marvel0-autonomy-end-dialog"
        title={t("marvel0.autonomy.end.title")}
        intro={t("marvel0.autonomy.end.intro")}
        hazards={[t("marvel0.autonomy.end.kills")]}
        notes={[
          t("marvel0.autonomy.gates_scope"),
          t("marvel0.autonomy.confirm.checks_window"),
        ]}
        attest={keyChecks("end").map((check) => ({
          code: check.code,
          label: t(check.label),
        }))}
        confirmLabel={t("marvel0.autonomy.end.button")}
        confirmIcon={CircleStop}
        danger
        ready={run.end.allowed && actions.working === null}
        working={actions.working === "end"}
        refused={
          refused ? (
            <ConfirmRefusal
              failure={refused.failure}
              testId="marvel0-autonomy-end-refused"
            >
              <WordedText worded={refused.worded} />
            </ConfirmRefusal>
          ) : null
        }
        onCancel={() => setAsking(false)}
        onConfirm={(ticked) =>
          actions
            .end(run.end.phrase, attestOf("end", ticked) ?? undefined)
            .then((ok) => {
              if (ok) setAsking(false);
            })
        }
      />
    </>
  );
}

function Facts({ run }: { run: RunView }) {
  const { t } = useLocale();
  const rows: Array<[string, string]> = [];
  if (run.d.alive && run.d.pid !== null)
    rows.push([t("marvel0.autonomy.fact.pid"), String(run.d.pid)]);
  if (run.d.alive)
    rows.push([
      t("marvel0.autonomy.fact.keyboard"),
      t(
        run.d.keyboard
          ? "marvel0.autonomy.fact.keyboard_yes"
          : "marvel0.autonomy.fact.keyboard_no",
      ),
    ]);
  if (run.d.alive && run.d.runnerAgeS !== null)
    rows.push([t("marvel0.autonomy.fact.heartbeat"), ago(run.d.runnerAgeS, t)]);
  const word = (value: string) =>
    wordOf(t, "marvel0.autonomy.lockstate.", value);
  if (run.monitor)
    rows.push([
      t("marvel0.autonomy.fact.monitor"),
      wordOf(t, "marvel0.autonomy.monitor.", run.monitor.state),
    ]);
  if (run.locks?.arm)
    rows.push([t("marvel0.autonomy.fact.arm_lock"), word(run.locks.arm)]);
  if (run.locks?.gpu)
    rows.push([t("marvel0.autonomy.fact.gpu_lock"), word(run.locks.gpu)]);
  for (const port of run.locks?.ports ?? [])
    rows.push([
      `${t("marvel0.autonomy.fact.port")} ${port.port}`,
      word(port.who),
    ]);
  if (rows.length === 0) return null;
  // Folded: it is for finding out why something is off. Open when D has no
  // keyboard on this page or the health monitor is not green.
  const odd = (run.d.alive && !run.d.keyboard) || run.monitor?.state === "red";
  return (
    <details
      className="rg-details rg-afactsfold"
      data-testid="marvel0-autonomy-facts-fold"
      open={odd || undefined}
    >
      <summary>{t("marvel0.autonomy.fact.summary")}</summary>
      <dl className="rg-facts rg-afacts" data-testid="marvel0-autonomy-facts">
        {rows.map(([label, value]) => (
          <div className="rg-fact" key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function ScreenTail({ run }: { run: RunView }) {
  const { t } = useLocale();
  if (!hasRun(run)) return null;
  return (
    <details
      className="rg-details rg-ascreen"
      data-testid="marvel0-autonomy-screen"
      open={run.screen.drift || undefined}
    >
      <summary>
        {sayText(
          {
            key: "marvel0.autonomy.screen.summary",
            params: { n: run.screen.tail.length },
          },
          t,
        )}
      </summary>
      {run.screen.drift && (
        <p className="rg-bad" role="status">
          {t("marvel0.autonomy.screen.drift")}
        </p>
      )}
      {/* D's own words, in the language it was started in. */}
      <pre
        className="rg-log"
        lang={run.profile?.consoleLang ?? undefined}
        tabIndex={0}
      >
        {run.screen.tail.join("\n")}
      </pre>
    </details>
  );
}

function History({ run }: { run: RunView }) {
  const { t } = useLocale();
  if (run.history.length === 0) return null;
  return (
    <details
      className="rg-details rg-ahist"
      data-testid="marvel0-autonomy-history"
    >
      <summary>
        {sayText(
          {
            key: "marvel0.autonomy.history.summary",
            params: { n: run.history.length },
          },
          t,
        )}
      </summary>
      <p className="rg-muted">{t("marvel0.autonomy.history.note")}</p>
      <ol className="rg-ahist__list">
        {run.history.map((row, index) => (
          <li key={`${row.rawTime}:${index}`}>
            <time dateTime={row.rawTime ?? undefined}>
              {row.at === null
                ? (row.rawTime ?? "")
                : new Date(row.at).toLocaleTimeString()}
            </time>{" "}
            {row.byte && <kbd className="ds-kbd">{row.byte}</kbd>}{" "}
            {wordOf(t, "marvel0.autonomy.key.", row.key)}
            {row.round !== null &&
              ` · ${sayText({ key: "marvel0.autonomy.history.round", params: { n: row.round } }, t)}`}
          </li>
        ))}
      </ol>
    </details>
  );
}

function Notices({ run }: { run: RunView }) {
  const { t } = useLocale();
  if (run.notices.length === 0) return null;
  return (
    <details
      className="rg-details"
      data-testid="marvel0-autonomy-notices"
      open={run.notices.some((n) => n.level !== "info") || undefined}
    >
      <summary>
        {t("marvel0.autonomy.notices").replace(
          "{n}",
          String(run.notices.length),
        )}
      </summary>
      <ul className="rg-notes">
        {run.notices.map((notice) => (
          <li
            key={notice.code}
            data-code={notice.code}
            data-level={notice.level}
          >
            {wordOf(t, "marvel0.autonomy.notice.", notice.code)}
          </li>
        ))}
      </ul>
    </details>
  );
}

function Ended({ run }: { run: RunView }) {
  const { t } = useLocale();
  const href = runPageHref(run);
  const kind = exitKind(run.d.exit, run.d.exitSignal);
  const after = run.afterExit;
  if (run.d.alive) return null;
  return (
    <div className="rg-aended" data-testid="marvel0-autonomy-ended">
      {kind && (
        <p data-testid="marvel0-autonomy-exit" data-kind={kind}>
          {sayText(
            {
              key: `marvel0.autonomy.exit.${kind}`,
              params: {
                code: run.d.exit ?? 0,
                signal: run.d.exitSignal ?? 0,
              },
            },
            t,
          )}
        </p>
      )}
      {after && (
        <ul className="rg-notes" data-testid="marvel0-autonomy-after">
          <li>
            {after.units === null
              ? t("marvel0.autonomy.after.units_unknown")
              : after.units.length === 0
                ? t("marvel0.autonomy.after.units_none")
                : sayText(
                    {
                      key: "marvel0.autonomy.after.units",
                      params: {
                        n: after.units.length,
                        names: after.units.join(", "),
                      },
                    },
                    t,
                  )}
          </li>
          {after.ports !== null && (
            <li>
              {after.ports.length === 0
                ? t("marvel0.autonomy.after.ports_none")
                : sayText(
                    {
                      key: "marvel0.autonomy.after.ports",
                      params: {
                        ports: after.ports.map((p) => p.port).join(", "),
                      },
                    },
                    t,
                  )}
            </li>
          )}
          {after.swapClean !== null && (
            <li>
              {t(
                after.swapClean
                  ? "marvel0.autonomy.after.swap_clean"
                  : "marvel0.autonomy.after.swap_dirty",
              )}
            </li>
          )}
        </ul>
      )}
      <p className="rg-muted">{t("marvel0.autonomy.labels_yours")}</p>
      {href && (
        <a
          className="ds-btn ds-btn--secondary ds-btn--md ds-focus rg-aend__link"
          href={href}
          data-testid="marvel0-autonomy-run-link"
        >
          <span className="ds-btn__label">
            {t("marvel0.autonomy.open_run")}
          </span>
          <ExternalLink size={14} aria-hidden="true" />
        </a>
      )}
    </div>
  );
}

function Queue({ run }: { run: RunView }) {
  const { t } = useLocale();
  if (run.mode !== "external" || !run.queue || !run.d.alive) return null;
  return (
    <p
      className="rg-aqueue"
      data-testid="marvel0-autonomy-queue"
      data-pending={run.queue.pending}
    >
      {run.queue.pending === 0
        ? t("marvel0.autonomy.queue.none")
        : sayText(
            {
              key: "marvel0.autonomy.queue.pending",
              params: {
                n: run.queue.pending,
                age: { ago: run.queue.oldestAgeS },
              },
            },
            t,
          )}{" "}
      <span className="rg-muted">{t("marvel0.autonomy.queue.note")}</span>
    </p>
  );
}

/** The edge colour of the card. Red for a D that cannot be trusted (stuck,
 * locked, unreadable) and for a Stop that D has not acknowledged: the arm may
 * still be moving then, and a green edge would say the opposite. */
const CARD_TONE = (run: RunView | null): string =>
  run === null || !hasRun(run)
    ? "not_started"
    : run.state.id === "stalled" ||
        run.state.id === "fault_locked" ||
        run.state.id === "unknown" ||
        (run.d.alive && stopUnacknowledged(run.stop.phase))
      ? "red"
      : mayMove(run)
        ? "ok"
        : run.d.alive
          ? "stale"
          : "not_started";

export function AutonomyRunCard({
  poll,
  actions,
  limits = LIMITS,
}: {
  poll: AutonomyRunPoll;
  actions: AutonomyActions;
  limits?: Limits;
}) {
  const { t } = useLocale();
  const run = poll.value;
  const arming = useArming(run);
  const press = useKeyPress(run, actions);
  const clockOn =
    run !== null &&
    (actions.sent !== null ||
      run.keys.some((key) => key.allowed && key.level !== "A"));
  const now = useClock(clockOn);
  const working = KEY_IDS.includes(actions.working as KeyId)
    ? (actions.working as KeyId)
    : null;
  const { lastOk: docAt, current } = poll;
  const { lastKeyAt, sent } = actions;
  const vms = useMemo(() => {
    if (!run) return [];
    const ctx: KeyCtx = {
      now,
      docAt,
      current,
      firstSeen: arming,
      lastKeyAt,
      sent,
      working,
      limits,
    };
    return visibleKeys(run).map((key) => keyVM(run, key, ctx));
  }, [run, now, docAt, current, arming, lastKeyAt, sent, working, limits]);
  const asked = press.asking
    ? (vms.find((vm) => vm.key.id === press.asking!.id) ?? null)
    : null;
  const refusedKey =
    actions.refused &&
    ![
      "stop",
      "end",
      "start",
      "prepare",
      "live_stop",
      "leftovers_stop",
    ].includes(actions.refused.key) &&
    !press.asking
      ? actions.refused
      : null;
  const noRun = poll.loaded && poll.value !== null && !hasRun(run);
  return (
    <Card
      padding="compact"
      tabIndex={-1}
      className={`rg-card rg-arun rg-card--${CARD_TONE(run)}`}
      data-testid="marvel0-autonomy-run"
      data-state={run?.state.id ?? undefined}
      title={t("marvel0.autonomy.run.title")}
      description={t("marvel0.autonomy.run.scope")}
      actions={
        <span className="rg-run__meta">
          <LiveAge
            lastOk={poll.lastOk}
            warnAfterMs={RUN_FRESH_MS / 2}
            testId="marvel0-autonomy-age"
          />
          <Button
            size="sm"
            variant="ghost"
            icon={RefreshCw}
            onClick={() => void poll.refresh()}
          >
            {t("Refresh")}
          </Button>
          {run && hasRun(run) && (
            <span data-testid="marvel0-autonomy-state">
              <Badge tone={STATE_TONE[run.state.id]}>
                {t(stateKey(run.state.id))}
              </Badge>
            </span>
          )}
        </span>
      }
    >
      {!run && poll.loaded && (
        <Notice
          tone="warning"
          role="status"
          testId="marvel0-autonomy-unreadable"
        >
          {t("marvel0.autonomy.run.unreadable")}{" "}
          {t(
            poll.lastSeen?.alive
              ? "marvel0.autonomy.run.unreadable_stop"
              : "marvel0.autonomy.run.unreadable_cli",
          )}{" "}
          <code lang="en">scripts/aeri_page_stop.py</code>
        </Notice>
      )}
      {run && !poll.current && (
        <Notice tone="warning" role="status" testId="marvel0-autonomy-stale">
          {t("marvel0.autonomy.run.stale")}
        </Notice>
      )}
      {noRun && (
        <p className="rg-muted" data-testid="marvel0-autonomy-norun">
          {t("marvel0.autonomy.run.none")}
        </p>
      )}
      {run && hasRun(run) && (
        <>
          <p className="rg-arun__who" data-testid="marvel0-autonomy-who">
            <code lang="en">{run.name}</code>
            {run.mode && <Badge tone="neutral">{t(modeKey(run.mode))}</Badge>}
            {run.profile && (
              <span data-testid="marvel0-autonomy-profile">
                <Badge tone="neutral">
                  {sayText(
                    {
                      key: "marvel0.autonomy.profile.badge",
                      params: { id: run.profile.id },
                    },
                    t,
                  )}
                </Badge>
              </span>
            )}
          </p>
          <StopFrame poll={poll} actions={actions} />
          <div
            className="rg-arun__status"
            data-testid="marvel0-autonomy-status"
          >
            <p className="rg-arun__hint" data-testid="marvel0-autonomy-hint">
              {t(
                stateHintKey(
                  run.state.id,
                  run.stop.phase,
                  run.state.reasonCode,
                  run.stop.allowed,
                ),
              )}
            </p>
            {run.state.reason && (
              <p className="rg-muted" data-testid="marvel0-autonomy-reason">
                <WordedText worded={run.state.reason} />
              </p>
            )}
            {run.state.since !== null && (
              <p className="rg-muted">
                {sayText(
                  {
                    key: "marvel0.autonomy.since",
                    params: {
                      ago: { ago: Math.max(0, (now - run.state.since) / 1000) },
                    },
                  },
                  t,
                )}
              </p>
            )}
            {promptLine(run) && (
              <blockquote
                className="rg-aprompt"
                data-testid="marvel0-autonomy-prompt"
                lang={run.profile?.consoleLang ?? undefined}
              >
                {promptLine(run)}
              </blockquote>
            )}
          </div>
          <Ended run={run} />
          <ProgressBlock run={run} />
          <Inventory run={run} />
          <WaitBlock run={run} />
          <KeysBlock run={run} vms={vms} onPress={press.press} />
          {refusedKey && (
            <RefusedBlock
              testId="marvel0-autonomy-refused"
              code={refusedKey.failure.code}
            >
              <WordedText worded={refusedKey.worded} />
            </RefusedBlock>
          )}
          <KeyConfirm
            asking={press.asking}
            run={run}
            vm={asked}
            actions={actions}
            onClose={press.close}
          />
          <EndForced run={run} actions={actions} />
          <Queue run={run} />
          <Facts run={run} />
          <ScreenTail run={run} />
          <History run={run} />
          <Notices run={run} />
          {isLive(run) && run.stopCli && (
            <p
              className="rg-muted rg-arun__cli"
              data-testid="marvel0-autonomy-cli"
            >
              {t("marvel0.autonomy.fallback")}{" "}
              <code lang="en">{run.stopCli}</code>
            </p>
          )}
        </>
      )}
    </Card>
  );
}
