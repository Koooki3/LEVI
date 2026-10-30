"use client";
// The Run card of the marvel0 (step 4): the evaluation client the page hosts.
// Top to bottom: its state and how far the episode is; while there is no
// client, how to start one (step 3) and why Start is off; while the arm may
// move, HOLD (the page's only solid danger button), full width and 64 px high
// in a place that never moves, with the one e-stop line beside it; the one key
// the state is waiting for (Start next episode, Success, Failure, Discard),
// all of equal weight and headed "moves the arm home"; a folded "Exit and go
// home (blind)" that opens its own confirm window; and, apart from HOLD at the
// foot of the card, how the run ends: End at the gate, or End run (a danger
// outline, confirm window) for a client that is starting, frozen or lost. No
// phrase is typed anywhere on the card: the page sends the fixed one.
//
// There is no `s` and no `p` anywhere here: at the client's start gate they
// run another task. A key is an action of run-logic.ts; the card never sends
// a raw key and has no keyboard shortcut of its own.
import { useState } from "react";
import {
  ArrowRight,
  CircleStop,
  Flag,
  Home,
  Lock,
  Pause,
  Play,
  RefreshCw,
  Snowflake,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import { Badge, Button, Card, Field, Input, Progress } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Notice } from "./notice";
import { RefusedBlock, WordedText } from "./action-parts";
import { ConfirmRefusal, ConfirmWindow } from "./confirm-window";
import { wordOf } from "./ctl-text";
import { COMMAND_ANCHOR, nodeAnchor } from "./chain-logic";
import { goTo } from "./chain-strip";
import { LiveAge } from "./live-age";
import { sayText } from "./marvel0-say";
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import type { KeyAction } from "./run-api";
import {
  ACTION_KEY,
  RUN_TONE,
  armedActions,
  holdEnabled,
  isQuiet,
  mayMove,
  runPhrase,
  runStateKey,
  stepBar,
  type ActionId,
  type RunView,
} from "./run-logic";
import type { RunPoll } from "./use-run-poll";
import { RUN_FRESH_MS } from "./use-run-poll";
import type { RunActions } from "./use-run-actions";
import { commandFormFromRun } from "./command-logic";

export type EpisodeChangeStatus =
  | { state: "idle" }
  | { state: "ending" | "waiting" | "restarting"; runId: string }
  | { state: "unknown" | "refused" | "failed"; runId: string }
  | { state: "ready"; runId: string };

/** The three things only a person can check before a client starts; the same
 * ticks as the start window (run-start.tsx). */
export const CHANGE_ACKS = ["estop", "scene_reset", "area_clear"] as const;
export type ChangeAttest = Record<(typeof CHANGE_ACKS)[number], boolean>;

const EPISODE_CHANGE_KEY: Record<
  Exclude<EpisodeChangeStatus["state"], "idle">,
  string
> = {
  ending: "marvel0.episodeChange.ending",
  waiting: "marvel0.episodeChange.waiting",
  restarting: "marvel0.episodeChange.restarting",
  unknown: "marvel0.episodeChange.unknown",
  refused: "marvel0.episodeChange.refused",
  failed: "marvel0.episodeChange.failed",
  ready: "marvel0.episodeChange.ready",
};

/** Notices of the backend that the card already says in its own place (the
 * caption beside HOLD, the keys' "moves the arm home"): not listed again. */
const SAID_ELSEWHERE = new Set(["hold_not_estop", "blind_home"]);

const ICON: Partial<Record<ActionId, typeof Play>> = {
  enter: Play,
  success: Flag,
  failure: X,
  discard: Trash2,
};

function ArmedActions({
  view,
  actions,
  enterOff = false,
}: {
  view: RunView;
  actions: RunActions;
  /** A new instruction waits in the gate's field: Enter would start the next
   * episode with the old one, so it is off until the field is applied or
   * cleared. */
  enterOff?: boolean;
}) {
  const { t } = useLocale();
  const ids = armedActions(view.state);
  if (ids.length === 0) return null;
  const blind = ids.some((id) => view.actions[id].blind);
  const whyNot: Array<{ ids: ActionId[]; why: Worded<Say> }> = [];
  for (const id of ids) {
    const { allowed, whyNot: why } = view.actions[id];
    if (allowed || !why) continue;
    const same = whyNot.find(
      (row) => JSON.stringify(row.why) === JSON.stringify(why),
    );
    if (same) same.ids.push(id);
    else whyNot.push({ ids: [id], why });
  }
  return (
    <div
      className="rg-run__armed"
      data-testid="marvel0-run-armed"
      data-state={view.state}
    >
      <div className="rg-run__keys-head">
        <h4 className="rg-marker__title">
          {t(
            view.state === "at_gate"
              ? "marvel0.run.keys.gate"
              : "marvel0.run.keys.label",
          )}
        </h4>
        {blind && (
          <Badge tone="warning" icon={Home}>
            {t("marvel0.run.keys.tag")}
          </Badge>
        )}
      </div>
      <div className="rg-run__keys">
        {ids.map((id) => {
          const action = view.actions[id];
          return (
            // All keys weigh the same: all of them send the arm home, and a
            // primary button would push the person towards one outcome.
            <Button
              key={id}
              size="lg"
              variant="secondary"
              icon={ICON[id]}
              data-action={id}
              loading={actions.working === id}
              disabled={
                !action.allowed ||
                actions.working !== null ||
                (id === "enter" && enterOff)
              }
              onClick={() => void actions.press(id as KeyAction)}
            >
              {t(ACTION_KEY[id])}
            </Button>
          );
        })}
      </div>
      {enterOff && ids.includes("enter") && (
        <p className="rg-block" data-testid="marvel0-run-enter-off">
          {t("marvel0.episodeChange.enterBlocked")}
        </p>
      )}
      {blind && (
        <p className="rg-muted" data-testid="marvel0-run-blind">
          {t("marvel0.run.blind_home")}
        </p>
      )}
      {whyNot.map(({ ids: offIds, why }) => (
        // Keys that are off for the same reason say it once.
        <p
          className="rg-block"
          key={offIds.join(",")}
          data-testid={`marvel0-run-why-${offIds[0]}`}
        >
          <span className="rg-block__label">
            {offIds.map((id) => t(ACTION_KEY[id])).join(", ")}:
          </span>{" "}
          <WordedText worded={why} />
        </p>
      ))}
    </div>
  );
}

/** The two checks of the blind exit: it moves the arm with no limit check, so
 * the person ticks that a hand is on the physical e-stop and the area is clear.
 * Ticks are not typing; they only switch Confirm on (the run's key request
 * carries the fixed phrase and no tick). */
const EXIT_ACKS = ["estop", "area_clear"] as const;

function ExitHome({ view, actions }: { view: RunView; actions: RunActions }) {
  const { t } = useLocale();
  const [asking, setAsking] = useState(false);
  const phrase = runPhrase(view, "exit_home");
  const action = view.actions.exit_home;
  const refused = actions.refused?.key === "exit_home" ? actions.refused : null;
  if (view.state !== "running") return null;
  return (
    <>
      <details
        className="rg-details rg-run__exit"
        data-testid="marvel0-run-exit"
      >
        <summary>{t("marvel0.run.exit_home.summary")}</summary>
        <p className="rg-muted">{t("marvel0.run.exit_home.hint")}</p>
        <Button
          variant="secondary"
          className="rg-btn--danger-outline"
          icon={Home}
          loading={actions.working === "exit_home"}
          disabled={!action.allowed || actions.working !== null}
          onClick={() => {
            actions.clear();
            setAsking(true);
          }}
        >
          {t(ACTION_KEY.exit_home)}
        </Button>
        {!action.allowed && action.whyNot && (
          <p className="rg-block">
            <WordedText worded={action.whyNot} />
          </p>
        )}
      </details>
      <ConfirmWindow
        open={asking}
        testId="marvel0-run-exit-dialog"
        title={t("marvel0.run.exit_home.summary")}
        intro={t("marvel0.run.exit_home.hint")}
        estop={t("marvel0.run.start.estop")}
        hazards={[t("marvel0.run.blind_home")]}
        notes={[t("marvel0.stack.gates_scope")]}
        attest={EXIT_ACKS.map((name) => ({
          code: name,
          label: wordOf(t, "marvel0.run.ack.", name),
        }))}
        confirmLabel={t(ACTION_KEY.exit_home)}
        confirmIcon={Home}
        danger
        ready={action.allowed && actions.working === null}
        working={actions.working === "exit_home"}
        refused={
          refused ? (
            <ConfirmRefusal
              failure={refused.failure}
              testId="marvel0-run-exit-refused"
            >
              <WordedText worded={refused.worded} />
            </ConfirmRefusal>
          ) : null
        }
        onCancel={() => setAsking(false)}
        onConfirm={() =>
          actions.press("exit_home", phrase).then((ok) => {
            if (ok) setAsking(false);
          })
        }
      />
    </>
  );
}

function EndDialog({
  open,
  view,
  actions,
  onClose,
}: {
  open: boolean;
  view: RunView;
  actions: RunActions;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const phrase = runPhrase(view, "end");
  const refused = actions.refused?.key === "end" ? actions.refused : null;
  return (
    <ConfirmWindow
      open={open}
      testId="marvel0-run-end-dialog"
      title={t("marvel0.run.end.title")}
      intro={t("marvel0.run.end.intro")}
      hazards={[t("marvel0.run.end.kills")]}
      notes={[t("marvel0.stack.gates_scope")]}
      confirmLabel={t(ACTION_KEY.end)}
      confirmIcon={CircleStop}
      danger
      ready={actions.working === null}
      working={actions.working === "end"}
      refused={
        refused ? (
          <ConfirmRefusal
            failure={refused.failure}
            testId="marvel0-run-end-refused"
          >
            <WordedText worded={refused.worded} />
          </ConfirmRefusal>
        ) : null
      }
      onCancel={onClose}
      onConfirm={() =>
        actions.end(phrase).then((ok) => {
          if (ok) onClose();
        })
      }
    />
  );
}

/** The reasons a run cannot start, in the block of "Start is off:" the cards
 * of the stack use; a warning that never refuses is a separate, quieter list. */
function StartOff({ view }: { view: RunView }) {
  const { t } = useLocale();
  const rows = view.preflight.filter((row) => row.level !== "ok");
  if (rows.length === 0) return null;
  const warns = rows.filter((row) => row.level === "warn");
  const off = rows.filter((row) => row.level !== "warn");
  const [first, ...rest] = off;
  const item = (row: (typeof rows)[number]) => (
    <li key={row.code} data-level={row.level} data-code={row.code}>
      <WordedText worded={row.worded} />
    </li>
  );
  return (
    <div data-testid="marvel0-run-preflight" className="rg-run__off">
      {first && (
        <div className="rg-block">
          <span className="rg-block__label">
            <Lock size={12} aria-hidden="true" />
            {t("marvel0.ctl.off.start")}
          </span>
          <ul className="rg-block__list">{item(first)}</ul>
          {rest.length > 0 && (
            <details className="rg-block__more">
              <summary>
                {t("marvel0.ctl.off.more").replace("{n}", String(rest.length))}
              </summary>
              <ul className="rg-block__list">{rest.map(item)}</ul>
            </details>
          )}
        </div>
      )}
      {warns.length > 0 && (
        <ul className="rg-preflight">
          {warns.map((row) => (
            <li key={row.code} data-level={row.level}>
              <TriangleAlert size={14} aria-hidden="true" />
              <span>
                <WordedText worded={row.worded} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function RunPanel({
  poll,
  actions,
  episodeChange = { state: "idle" },
  onChangeInstruction,
}: {
  poll: RunPoll;
  actions: RunActions;
  episodeChange?: EpisodeChangeStatus;
  onChangeInstruction?: (prompt: string, attest: ChangeAttest) => void;
}) {
  const { t } = useLocale();
  const [ending, setEnding] = useState(false);
  const [changeOpen, setChangeOpen] = useState(false);
  const [nextPrompt, setNextPrompt] = useState("");
  const view = poll.value;
  const state = view?.state ?? null;
  // The field belongs to one run: a new client starts with it empty.
  const runId = view?.run?.runId ?? null;
  const [promptRun, setPromptRun] = useState<string | null>(runId);
  if (promptRun !== runId) {
    setPromptRun(runId);
    setNextPrompt("");
    setChangeOpen(false);
  }
  const currentPrompt =
    typeof view?.run?.form?.prompt === "string" ? view.run.form.prompt : null;
  const wanted = nextPrompt.trim();
  const changing =
    onChangeInstruction !== undefined &&
    state === "at_gate" &&
    wanted !== "" &&
    wanted !== currentPrompt;
  const canChange =
    changing &&
    poll.current &&
    !!view?.run?.runId &&
    commandFormFromRun(view.run.form) !== null &&
    view.actions.end_at_gate.allowed &&
    actions.working === null &&
    episodeChange.state === "idle";
  // HOLD goes by the last state seen, however old: a client that was running
  // when the answers stop is the case in which it matters most.
  const holdOn = holdEnabled(poll.lastSeen);
  const frozen = state === "held";
  const bar = view ? stepBar(view.episode) : null;
  const refusedHere =
    actions.refused && actions.refused.key !== "end" ? actions.refused : null;
  const notices = (view?.notices ?? []).filter(
    (code) => !SAID_ELSEWHERE.has(code),
  );
  const endInFrame = !holdOn && frozen;
  const endRunButtons = (
    <>
      <Button
        variant="secondary"
        className="rg-btn--danger-outline"
        icon={CircleStop}
        onClick={() => setEnding(true)}
      >
        {t(ACTION_KEY.end)}
      </Button>
      <span className="rg-muted">
        {t(frozen ? "marvel0.run.end_hint" : "marvel0.run.end_hint_live")}
      </span>
    </>
  );
  const endRun = (
    <div className="rg-run__end" data-testid="marvel0-run-end">
      {endRunButtons}
    </div>
  );
  const endable =
    view !== null &&
    (view.state === "held" ||
      view.state === "lost" ||
      view.state === "starting") &&
    view.actions.end.allowed;
  // No client yet (or the last one is over): the card says how to start one
  // instead of showing a HOLD frame with nothing to hold.
  const noClient =
    view !== null &&
    view.enabled &&
    (isQuiet(view.state) || state === "preflight");
  return (
    <Card
      padding="compact"
      id={nodeAnchor("run")}
      tabIndex={-1}
      className={`rg-card rg-run rg-card--${
        state === null
          ? "unknown"
          : state === "failed" || state === "lost"
            ? "red"
            : state === "held" ||
                state === "awaiting_label" ||
                state === "homing"
              ? "stale"
              : mayMove(state)
                ? "ok"
                : "not_started"
      }`}
      data-testid="marvel0-run"
      data-state={state ?? undefined}
      title={t("marvel0.run.title")}
      description={t("marvel0.run.scope")}
      actions={
        <span className="rg-run__meta">
          <LiveAge
            lastOk={poll.lastOk}
            warnAfterMs={RUN_FRESH_MS / 2}
            testId="marvel0-run-age"
          />
          <Button
            size="sm"
            variant="ghost"
            icon={RefreshCw}
            onClick={() => void poll.refresh()}
          >
            {t("Refresh")}
          </Button>
          {view && (
            <Badge tone={RUN_TONE[view.state]}>
              {t(runStateKey(view.state))}
            </Badge>
          )}
        </span>
      }
    >
      {!view && poll.loaded && (
        <Notice tone="warning" role="status" testId="marvel0-run-unreadable">
          {t("marvel0.run.unreadable")}
        </Notice>
      )}
      {view && !poll.current && (
        <Notice tone="warning" role="status" testId="marvel0-run-stale">
          {t("marvel0.run.stale")}
        </Notice>
      )}
      {view && !view.enabled && (
        <Notice tone="info" testId="marvel0-run-disabled">
          {t("marvel0.run.disabled")}
        </Notice>
      )}
      <div className={`rg-run__status${holdOn ? " rg-run__status--hold" : ""}`}>
        {view?.reason && (
          <p className="rg-run__reason" data-testid="marvel0-run-reason">
            <WordedText worded={view.reason} />
          </p>
        )}
        {view?.episode && view.episode.no !== null && (
          <p className="rg-run__episode" data-testid="marvel0-run-episode">
            {sayText(
              {
                key:
                  view.episode.target === null
                    ? "marvel0.run.episode"
                    : "marvel0.run.episode_of",
                params: {
                  no: view.episode.no,
                  target: view.episode.target ?? 0,
                  counted: view.episode.counted ?? 0,
                },
              },
              t,
            )}
          </p>
        )}
        {bar && (
          <Progress
            value={bar.value}
            max={bar.max}
            label={sayText(
              {
                key: "marvel0.run.steps",
                params: { step: bar.value, max: bar.max },
              },
              t,
            )}
          />
        )}
      </div>

      {noClient && view && (
        <>
          <div className="rg-run__how" data-testid="marvel0-run-how">
            <ArrowRight size={16} aria-hidden="true" />
            <span>{t("marvel0.run.idle.how")}</span>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => goTo(COMMAND_ANCHOR)}
            >
              {t("marvel0.run.idle.go")}
            </Button>
          </div>
          <StartOff view={view} />
        </>
      )}

      {holdOn && (
        <div className="rg-run__hold" data-testid="marvel0-run-hold-frame">
          <Button
            variant="danger"
            size="lg"
            className="rg-hold"
            icon={Pause}
            onClick={() => void actions.hold()}
            data-testid="marvel0-run-hold"
          >
            {t("marvel0.run.hold")}
          </Button>
          <p className="rg-hold__caption" role="status">
            {actions.holding
              ? t("marvel0.run.hold_sending")
              : t("marvel0.run.hold_caption")}
          </p>
        </div>
      )}
      {endInFrame && (
        // HOLD has done its work: the frame says so and holds the one action
        // that is left, End (the state's reason above says the rest).
        <div
          className="rg-run__frozen"
          role="status"
          data-testid="marvel0-run-frozen"
        >
          <Snowflake size={18} aria-hidden="true" />
          <strong>{t(runStateKey("held"))}</strong>
          {endRun}
        </div>
      )}

      {view && !frozen && (
        <ArmedActions view={view} actions={actions} enterOff={changing} />
      )}
      {view && <ExitHome view={view} actions={actions} />}
      {refusedHere && (
        <RefusedBlock
          testId="marvel0-run-refused"
          code={refusedHere.failure.code}
        >
          <WordedText worded={refusedHere.worded} />
        </RefusedBlock>
      )}
      {view && state === "at_gate" && (
        // How the run ends at the gate, apart from HOLD and from the key above.
        <div className="rg-run__end" data-testid="marvel0-run-end">
          <Button
            variant="secondary"
            icon={CircleStop}
            loading={actions.working === "end_at_gate"}
            disabled={
              !view.actions.end_at_gate.allowed || actions.working !== null
            }
            onClick={() => void actions.press("end_at_gate")}
          >
            {t(ACTION_KEY.end_at_gate)}
          </Button>
          <span className="rg-muted">{t("marvel0.run.end_at_gate_hint")}</span>
        </div>
      )}
      {view && state === "at_gate" && onChangeInstruction && (
        <section
          className="rg-run__episode-change"
          data-testid="marvel0-episode-change"
        >
          {episodeChange.state === "idle" ? (
            <>
              <Field label={t("marvel0.episodeChange.promptLabel")}>
                <Input
                  autoComplete="off"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  value={nextPrompt}
                  placeholder={currentPrompt ?? ""}
                  onChange={(event) => setNextPrompt(event.target.value)}
                  data-testid="marvel0-episode-change-prompt"
                />
              </Field>
              {currentPrompt !== null && (
                <p className="rg-muted" data-testid="marvel0-episode-current">
                  {t("marvel0.episodeChange.current").replace(
                    "{prompt}",
                    currentPrompt,
                  )}
                </p>
              )}
              {changing && (
                <div className="rg-actions-row">
                  <Button
                    variant="primary"
                    icon={RefreshCw}
                    disabled={!canChange}
                    onClick={() => setChangeOpen(true)}
                    data-testid="marvel0-episode-change-open"
                  >
                    {t("marvel0.episodeChange.restart")}
                  </Button>
                  <Button variant="ghost" onClick={() => setNextPrompt("")}>
                    {t("marvel0.episodeChange.keep")}
                  </Button>
                </div>
              )}
              {changing &&
                (!view.run?.runId || !commandFormFromRun(view.run.form)) && (
                  <p className="rg-warn" role="status">
                    {t("marvel0.episodeChange.formUnavailable")}
                  </p>
                )}
              <ConfirmWindow
                open={changeOpen && canChange}
                testId="marvel0-episode-change-dialog"
                title={t("marvel0.episodeChange.restart")}
                intro={t("marvel0.episodeChange.explanation")}
                facts={
                  <dl className="rg-facts">
                    <div className="rg-fact">
                      <dt>{t("marvel0.episodeChange.from")}</dt>
                      <dd>{currentPrompt ?? ""}</dd>
                    </div>
                    <div className="rg-fact">
                      <dt>{t("marvel0.episodeChange.to")}</dt>
                      <dd data-testid="marvel0-episode-change-to">{wanted}</dd>
                    </div>
                  </dl>
                }
                estop={t("marvel0.run.start.estop")}
                hazards={[t("marvel0.run.start.first_enter")]}
                notes={[t("marvel0.stack.gates_scope")]}
                attest={CHANGE_ACKS.map((name) => ({
                  code: name,
                  label: wordOf(t, "marvel0.run.ack.", name),
                }))}
                confirmLabel={t("marvel0.episodeChange.go")}
                confirmIcon={RefreshCw}
                ready={canChange}
                onCancel={() => setChangeOpen(false)}
                onConfirm={(ticked) => {
                  setChangeOpen(false);
                  onChangeInstruction(wanted, {
                    estop: ticked.estop === true,
                    scene_reset: ticked.scene_reset === true,
                    area_clear: ticked.area_clear === true,
                  });
                }}
              />
            </>
          ) : (
            <p
              role="status"
              className={
                episodeChange.state === "unknown" ||
                episodeChange.state === "failed"
                  ? "rg-warn"
                  : "rg-muted"
              }
              data-testid="marvel0-episode-change-status"
              data-state={episodeChange.state}
            >
              {t(EPISODE_CHANGE_KEY[episodeChange.state])}
            </p>
          )}
        </section>
      )}
      {view &&
        state !== null &&
        state !== "at_gate" &&
        ["running", "awaiting_label", "homing"].includes(state) && (
          <p className="rg-muted" data-testid="marvel0-episode-change-blocked">
            {t("marvel0.episodeChange.finishCurrent")}
          </p>
        )}
      {episodeChange.state !== "idle" && (!view || state !== "at_gate") && (
        <p
          role="status"
          className={
            episodeChange.state === "unknown" ||
            episodeChange.state === "failed"
              ? "rg-warn"
              : "rg-muted"
          }
          data-testid="marvel0-episode-change-status"
          data-state={episodeChange.state}
        >
          {t(EPISODE_CHANGE_KEY[episodeChange.state])}
        </p>
      )}
      {view && state !== "at_gate" && (frozen || endable) && !endInFrame && (
        // How a client that is starting or lost ends, apart from HOLD.
        <div className="rg-run__end" data-testid="marvel0-run-end">
          {endRunButtons}
        </div>
      )}
      {view && notices.length > 0 && (
        // What the gates are worth and what the keys do, in the backend's
        // codes: folded away, the HOLD caption already says the part that
        // matters while the arm may move.
        <details className="rg-details rg-run__notices">
          <summary>
            {t("marvel0.run.notices").replace("{n}", String(notices.length))}
          </summary>
          <ul className="rg-notes" data-testid="marvel0-run-notices">
            {notices.map((code) => (
              <li key={code} data-code={code}>
                {wordOf(t, "marvel0.run.notice.", code)}
              </li>
            ))}
          </ul>
        </details>
      )}
      {view && (
        <EndDialog
          open={ending}
          view={view}
          actions={actions}
          onClose={() => setEnding(false)}
        />
      )}
    </Card>
  );
}
