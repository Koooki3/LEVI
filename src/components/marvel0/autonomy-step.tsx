"use client";
// Step 5 of the marvel0 page: the autonomy system. The loop of AERI is a
// vision model that plans subtasks, the policy that executes them, a judge that
// decides whether each worked and a reverse loop that resets the scene; this
// step prepares a run, starts the terminal D that runs it inside this page and
// shows it, and lets a person press D's keys with a click. A click stands for
// the same key in the terminal; the terminal stays a valid way to run AERI.
//
// Everything on this step is off until the control file says so
// (`[aeri] enabled = true`; a missing section is off, a malformed one is off
// with an error, never on). Only a person clicks: an agent never calls the
// write routes behind these buttons (in external mode it only answers the
// vision-model queue files). The first real use is staged, with the owner
// present and a hand on the physical e-stop.
//
// The step owns the polls and the actions; the cards are views of them. Stop
// has a dock (like the evaluation client's HOLD) for the person who has
// scrolled away from the card while D lives.
import { useEffect, useState } from "react";
import "./autonomy-styles.css";
import { ArrowDown, Square } from "lucide-react";
import { Badge, Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { WordedText } from "./action-parts";
import { autonomyApi, type AutonomyApi } from "./autonomy-api";
import { AutonomyRunCard } from "./autonomy-run-card";
import { PrepareCard } from "./autonomy-prepare";
import {
  LIMITS,
  STATE_TONE,
  hasRun,
  isLive,
  stateKey,
  stopControl,
  type Limits,
} from "./autonomy-logic";
import { StepHeading } from "./marvel0-card";
import { sayFailure } from "./marvel0-failure";
import {
  newTabId,
  useAutonomyActions,
  useAutonomyReady,
  useAutonomyRun,
  usePresence,
} from "./use-autonomy";

/** Why hosting is off: the control file has no `[aeri]` section (the default),
 * has a malformed one (off, with its errors), or says `enabled = false`. */
const disabledKey = (state: string | undefined): string =>
  state === "missing"
    ? "marvel0.autonomy.disabled.missing"
    : state === "invalid"
      ? "marvel0.autonomy.disabled.invalid"
      : "marvel0.autonomy.disabled.off";

const CARD_STOP = '[data-testid="marvel0-autonomy-stop-frame"]';
/** About how high the dock is: the card's Stop only counts as on screen when
 * it is clear of the dock's place. */
const DOCK_PX = 96;

function useCardStopOnScreen(): boolean {
  const [onScreen, setOnScreen] = useState(false);
  useEffect(() => {
    let frame = 0;
    const look = () => {
      frame = 0;
      const el = document.querySelector(CARD_STOP);
      if (!el) return setOnScreen(false);
      const box = el.getBoundingClientRect();
      setOnScreen(box.bottom > 0 && box.top < window.innerHeight - DOCK_PX);
    };
    const queue = () => {
      if (!frame) frame = requestAnimationFrame(look);
    };
    look();
    window.addEventListener("scroll", queue, { passive: true });
    window.addEventListener("resize", queue);
    const timer = setInterval(look, 1000);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      clearInterval(timer);
      window.removeEventListener("scroll", queue);
      window.removeEventListener("resize", queue);
    };
  }, []);
  return onScreen;
}

/** Stop in a bar pinned to the bottom of the window while D lives and the
 * card's own Stop is out of view. The same Stop (`actions.stop`), by the same
 * rule (the last good read says it is offered, however old that read is). */
function StopDock({
  poll,
  actions,
}: {
  poll: ReturnType<typeof useAutonomyRun>;
  actions: ReturnType<typeof useAutonomyActions>;
}) {
  const { t } = useLocale();
  const cardOnScreen = useCardStopOnScreen();
  const { mode, enabled } = stopControl(poll.lastSeen, actions.stopSent);
  const run = poll.value;
  if (mode === "hidden" || cardOnScreen) return null;
  return (
    <section
      className="rg-dock"
      aria-label={t("marvel0.autonomy.dock.label")}
      data-testid="marvel0-autonomy-dock"
      data-mode={mode}
    >
      <div className="rg-dock__info">
        <div className="rg-dock__line">
          {run && (
            <Badge tone={STATE_TONE[run.state.id]}>
              {t(stateKey(run.state.id))}
            </Badge>
          )}
          {!poll.current && (
            <span className="rg-dock__stale">
              {t("marvel0.autonomy.run.stale")}
            </span>
          )}
        </div>
        <p className="rg-dock__note">{t("marvel0.autonomy.dock.note")}</p>
      </div>
      <Button
        variant="secondary"
        size="lg"
        icon={ArrowDown}
        className="rg-dock__hold"
        onClick={() =>
          document
            .querySelector("#marvel0-step-autonomy")
            ?.scrollIntoView?.({ block: "start" })
        }
        data-testid="marvel0-autonomy-dock-go"
      >
        {t("marvel0.autonomy.dock.go")}
      </Button>
      <Button
        variant="danger"
        size="lg"
        icon={Square}
        className="rg-dock__hold"
        disabled={!enabled}
        loading={actions.stopping}
        onClick={() => void actions.stop()}
        data-testid="marvel0-autonomy-dock-stop"
      >
        {t("marvel0.autonomy.stop.button")}
      </Button>
    </section>
  );
}

export function AutonomyStep({
  api = autonomyApi,
  evalLive = false,
  limits = LIMITS,
}: {
  api?: AutonomyApi;
  /** The hosted evaluation client is live (Step 4): only one of the two
   * sessions can be alive, so Start here is off meanwhile. */
  evalLive?: boolean;
  limits?: Limits;
}) {
  const { t } = useLocale();
  const [tabId] = useState(newTabId);
  const ready = useAutonomyReady(api, { pollMs: limits.readyPollMs });
  const run = useAutonomyRun(api, limits);
  const actions = useAutonomyActions(api, run, ready, { tabId });
  const runView = run.value;
  const runLive = isLive(runView);
  usePresence(api, runLive, tabId, limits);
  // Off in the control file: the routes answer `aeri_disabled`, or the
  // document says `enabled: false`. A D that is still alive keeps its card.
  const coded = (problem: typeof ready.problem, code: string) =>
    problem !== null && !("format" in problem) && problem.code === code;
  const disabled =
    ready.value?.enabled === false ||
    coded(ready.problem, "aeri_disabled") ||
    coded(run.problem, "aeri_disabled");
  // The site file has no `[aeri]` section, or a malformed one: nothing can be
  // prepared. Said apart from "off", which is the control file's switch.
  const siteState = ready.value?.site.state ?? "ok";
  const unconfigured = ready.value !== null && siteState !== "ok";
  const showRun = hasRun(runView) || (run.loaded && runView === null);
  const readProblem =
    ready.problem && !("format" in ready.problem) && !disabled
      ? ready.problem
      : null;
  return (
    <>
      <div className="rg-steps" data-testid="marvel0-autonomy">
        <section
          className="rg-section rg-step"
          aria-labelledby="marvel0-step-autonomy"
          data-testid="marvel0-step-autonomy"
        >
          <StepHeading n={5} id="marvel0-step-autonomy">
            {t("marvel0.autonomy.step")}
          </StepHeading>
          <p className="rg-step__lead" data-testid="marvel0-autonomy-lead">
            {t("marvel0.autonomy.lead")}
          </p>
          {/* Always shown, never folded: what these buttons are and are not. */}
          <Note tone="warning" className="rg-scope-note">
            <strong data-testid="marvel0-autonomy-honest">
              {t("marvel0.autonomy.honest")}
            </strong>
          </Note>
          {disabled && (
            <Note tone="info" role="status">
              <span
                data-testid="marvel0-autonomy-disabled"
                data-control={ready.value?.controlState}
              >
                {t(disabledKey(ready.value?.controlState))}
              </span>
            </Note>
          )}
          {unconfigured && !disabled && (
            <Note
              tone={siteState === "invalid" ? "warning" : "info"}
              role="status"
            >
              <span
                data-testid="marvel0-autonomy-unconfigured"
                data-site={siteState}
              >
                {t(
                  siteState === "invalid"
                    ? "marvel0.autonomy.site_invalid"
                    : "marvel0.autonomy.unconfigured",
                )}
                {ready.value?.site.errors.map((error) => (
                  <code key={`${error.field}:${error.code}`} lang="en">
                    {" "}
                    {error.field ? `${error.field}: ` : ""}
                    {error.code}
                  </code>
                ))}
              </span>
            </Note>
          )}
          {readProblem && (
            <Note tone="warning" role="status">
              <span data-testid="marvel0-autonomy-read-problem">
                {t("marvel0.autonomy.ready.unreadable")}{" "}
                <WordedText worded={sayFailure(readProblem)} />
              </span>
            </Note>
          )}
          {!ready.loaded && !run.loaded && (
            <p role="status" aria-busy="true" className="rg-muted">
              {t("marvel0.act.reading")}
            </p>
          )}
          <div className="rg-autonomy__cards">
            {showRun && !(disabled && !runLive) && (
              <AutonomyRunCard poll={run} actions={actions} limits={limits} />
            )}
            {!runLive && !disabled && !unconfigured && ready.loaded && (
              <PrepareCard
                poll={ready}
                actions={actions}
                evalLive={evalLive}
                runLive={runLive}
                run={runView}
              />
            )}
          </div>
        </section>
      </div>
      {/* A child of the page, not of the step: a sticky bar is held inside its
          parent, and the step is out of view when the dock is needed. */}
      <StopDock poll={run} actions={actions} />
    </>
  );
}
