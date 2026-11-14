"use client";
// The HOLD dock: a bar pinned to the bottom of the window with HOLD in it,
// shown while the hosted client may move the arm and the Run card's own HOLD
// is not on screen. The Run card sits at the very end of a long page; a person
// who has scrolled up to the chain, the arm stack or the policy server while a
// run goes on must never have to look for the one button that freezes it.
//
// It is the same HOLD as the card's (`actions.hold`: no challenge, no phrase,
// sent at once), by the same rule (`holdEnabled`: the last good read said the
// arm may move, however old that read is). It is hidden while the card's HOLD
// is on screen, so that two HOLD buttons never compete in one picture, and it
// is `position: sticky` at the end of the page rather than `fixed`, so that it
// never covers the last cards. It does not say this is an emergency stop: it
// says the opposite, in the line beside the button.
import { useEffect, useId, useRef, useState } from "react";
import { ArrowDown, Pause } from "lucide-react";
import { Badge, Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { RefusedBlock, WordedText } from "./action-parts";
import { goToNode } from "./chain-strip";
import { sayText } from "./marvel0-say";
import { RUN_TONE, holdEnabled, runStateKey, stepBar } from "./run-logic";
import type { RunActions } from "./use-run-actions";
import type { RunPoll } from "./use-run-poll";

/** The Run card's HOLD (run-panel.tsx), or, once the client is frozen and
 * there is no HOLD left, the frame that says so and holds End. */
const CARD_HOLD =
  '[data-testid="marvel0-run-hold"], [data-testid="marvel0-run-frozen"]';
/** About how high the dock is: the card's HOLD only counts as on screen when
 * it is clear of the dock's place. The dock's own height is read when it has
 * been shown; this is the first guess. */
const DOCK_PX = 96;

/** The sticky top bar's height, as the page's own custom property says. */
function stickyTop(): number {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue("--levi-sticky-top")
    .trim();
  const value = raw.endsWith("px") ? parseFloat(raw) : 0;
  return Number.isFinite(value) ? value : 0;
}

/** Whether the Run card's HOLD can be pressed now: all of it inside the
 * window, below the top bar and above the dock's place. A button that is
 * partly under the bar or the dock does not count, or both would be
 * unusable for a moment. Looked at on scroll and resize and once a second
 * (the card moves when the page above it grows). */
function useCardHoldOnScreen(dockPx: number): boolean {
  const [onScreen, setOnScreen] = useState(false);
  useEffect(() => {
    let frame = 0;
    const look = () => {
      frame = 0;
      const el = document.querySelector(CARD_HOLD);
      if (!el) return setOnScreen(false);
      const box = el.getBoundingClientRect();
      setOnScreen(
        box.height > 0 &&
          box.top >= stickyTop() &&
          box.bottom <= window.innerHeight - dockPx,
      );
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
  }, [dockPx]);
  return onScreen;
}

export function RunDock({
  poll,
  actions,
}: {
  poll: RunPoll;
  actions: RunActions;
}) {
  const { t } = useLocale();
  const dock = useRef<HTMLElement>(null);
  const [dockPx, setDockPx] = useState(DOCK_PX);
  useEffect(() => {
    const height = dock.current?.offsetHeight ?? 0;
    if (height > 0 && height !== dockPx) setDockPx(height);
  });
  const cardHoldOnScreen = useCardHoldOnScreen(dockPx);
  const noteId = useId();
  const seen = poll.lastSeen;
  // A frozen client keeps the dock too: after the click on HOLD the person
  // who is far from the Run card must see that it worked, and be shown where
  // the only action left (End) is.
  const frozen = seen?.state === "held";
  // Lost, or not read at all: HOLD may not reach the client; the line says so.
  const alarm = seen === null || seen.state === "lost";
  // Nothing read yet because the read failed (a reload while a client runs,
  // the backend busy): HOLD is still offered, and the dock says it does not
  // know. HOLD is idempotent and refuses by itself when nothing is live.
  const unknown = seen === null && poll.loaded && poll.problem !== null;
  if (cardHoldOnScreen) return null;
  if (!unknown && (seen === null || !(holdEnabled(seen) || frozen)))
    return null;
  const view = poll.value;
  const holdRefused =
    actions.refused && actions.refused.key === "hold" ? actions.refused : null;
  const bar = view ? stepBar(view.episode) : null;
  const episode = view?.episode;
  return (
    <section
      ref={dock}
      id="marvel0-run-dock"
      className={`rg-dock${frozen ? " rg-dock--frozen" : ""}`}
      aria-label={t("marvel0.run.dock.label")}
      data-testid="marvel0-run-dock"
      data-state={seen?.state ?? "unknown"}
    >
      <div className="rg-dock__info">
        <div className="rg-dock__line">
          {seen ? (
            <Badge tone={RUN_TONE[seen.state]}>
              {t(runStateKey(seen.state))}
            </Badge>
          ) : (
            <Badge tone="warning">{t("marvel0.run.dock.unknown_state")}</Badge>
          )}
          {episode && episode.no !== null && (
            <span className="rg-dock__where" data-testid="marvel0-run-dock-at">
              {sayText(
                {
                  key:
                    episode.target === null
                      ? "marvel0.run.episode"
                      : "marvel0.run.episode_of",
                  params: {
                    no: episode.no,
                    target: episode.target ?? 0,
                    counted: episode.counted ?? 0,
                  },
                },
                t,
              )}
              {bar &&
                ` · ${sayText(
                  {
                    key: "marvel0.run.steps",
                    params: { step: bar.value, max: bar.max },
                  },
                  t,
                )}`}
            </span>
          )}
          {!poll.current && (
            <span
              className="rg-dock__stale"
              data-testid="marvel0-run-dock-stale"
            >
              {t("marvel0.run.dock.stale")}
            </span>
          )}
        </div>
        {frozen ? (
          <p className="rg-dock__note" data-testid="marvel0-run-dock-frozen">
            {view?.reason ? (
              // The reason of a frozen client already says that nothing
              // resumes; the plain sentence is for a frozen client without one.
              <WordedText worded={view.reason} />
            ) : (
              t("marvel0.run.hold_done")
            )}
          </p>
        ) : (
          // A client that is lost may still be alive but the page cannot say
          // so: HOLD is still offered, and the line beside it says it may not
          // get there.
          <p
            id={noteId}
            className={`rg-dock__note${alarm ? " rg-dock__note--lost" : ""}`}
            role={alarm ? "alert" : undefined}
            data-testid={
              seen?.state === "lost"
                ? "marvel0-run-dock-lost"
                : seen === null
                  ? "marvel0-run-dock-unknown"
                  : undefined
            }
          >
            {t(
              seen?.state === "lost"
                ? "marvel0.run.dock.lost"
                : seen === null
                  ? "marvel0.run.dock.unknown"
                  : "marvel0.run.dock.note",
            )}
          </p>
        )}
        {holdRefused && (
          // The Run card, which would show this, is out of view by definition
          // while the dock is shown: the refusal is said here, at once.
          <RefusedBlock
            testId="marvel0-run-dock-refused"
            code={holdRefused.failure.code}
          >
            <WordedText worded={holdRefused.worded} />
          </RefusedBlock>
        )}
        {actions.holding && !frozen && (
          <p
            className="rg-dock__sending"
            role="status"
            data-testid="marvel0-run-dock-sending"
          >
            {t("marvel0.run.hold_sending")}
          </p>
        )}
      </div>
      {frozen ? (
        <Button
          variant="secondary"
          size="lg"
          icon={ArrowDown}
          className="rg-dock__hold"
          onClick={() => goToNode("run")}
          data-testid="marvel0-run-dock-go"
        >
          {t("marvel0.run.dock.go")}
        </Button>
      ) : (
        <Button
          variant="danger"
          size="lg"
          icon={Pause}
          className="rg-dock__hold"
          aria-describedby={noteId}
          onClick={() => void actions.hold()}
          data-testid="marvel0-run-dock-hold"
        >
          {t("marvel0.run.hold")}
        </Button>
      )}
    </section>
  );
}
