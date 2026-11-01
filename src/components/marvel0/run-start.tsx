"use client";
// "Start the client", under the command the command card made: it starts
// the very client that command describes, hosted by LEVI (run-panel.tsx drives
// it afterwards). One press starts it: a challenge for this form, then the
// start. There is no window and there are no tick boxes (owner request
// 2026-11-01); the press itself is the person's statement of the three things
// only a person can check (a hand is on the physical e-stop, the scene is
// reset, the area is clear), and the line under the button says so, next to
// the e-stop warning. The page sends the confirmation phrase and those three
// statements itself; the backend still requires and checks both. The copy of
// the command stays as the manual route. The checks stop mistakes, not another
// program of the same account.
//
// The press is the only thing that starts it: nothing here starts on its own,
// and the button is off while a write is on its way (the actions' own busy
// guard is a second layer) and wherever the backend says the client cannot be
// started. Voice run and the instruction change start the client through
// `actions.start` with the ticks their own windows asked for, not from here.
import { Play } from "lucide-react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { WordedText } from "./action-parts";
import { ConfirmRefusal } from "./confirm-window";
import { startPhrase, type RunView } from "./run-logic";
import type { Ack, RunActions } from "./use-run-actions";

/** What one press of Start states, sent as the `attest` of the start. */
export const PRESS_STATES: Readonly<Ack> = Object.freeze({
  estop: true,
  scene_reset: true,
  area_clear: true,
});

export function RunStart({
  view,
  actions,
  form,
  group,
  stale,
}: {
  /** The hosted client's state; null while it is not read. */
  view: RunView | null;
  actions: RunActions;
  /** The form of the command, as the preview request sent it. */
  form: Record<string, unknown>;
  /** The rollout group of this run: the phrase names it. */
  group: string;
  /** The form changed after the command was made: it is made again first. */
  stale: boolean;
}) {
  const { t } = useLocale();
  const action = view?.actions.start ?? null;
  const phrase = startPhrase(view, group);
  const working = actions.working !== null;
  const refused = actions.refused?.key === "start" ? actions.refused : null;
  const off = !action?.allowed || stale || working;
  return (
    <div className="rg-runstart" data-testid="marvel0-run-start">
      <Button
        variant="primary"
        icon={Play}
        disabled={off}
        loading={actions.working === "start"}
        onClick={() => {
          actions.clear();
          void actions.start(form, phrase, { ...PRESS_STATES });
        }}
      >
        {t("marvel0.run.start.button")}
      </Button>
      {stale ? (
        <span className="rg-muted">{t("marvel0.run.start.stale")}</span>
      ) : !view ? (
        <span className="rg-muted">{t("marvel0.run.unreadable")}</span>
      ) : !action?.allowed && action?.whyNot ? (
        <span className="rg-block" data-testid="marvel0-run-start-why">
          <WordedText worded={action.whyNot} />
        </span>
      ) : null}
      <div
        className="rg-runstart__line"
        data-testid="marvel0-run-start-warning"
      >
        <Note tone="warning">
          <span data-testid="marvel0-run-start-means">
            {t("marvel0.run.start.means")}
          </span>{" "}
          {t("marvel0.run.start.estop")}
        </Note>
        <p className="rg-muted">{t("marvel0.run.start.first_enter")}</p>
      </div>
      {refused && (
        <div className="rg-runstart__line">
          <ConfirmRefusal
            failure={refused.failure}
            testId="marvel0-run-start-refused"
          >
            <WordedText worded={refused.worded} />
          </ConfirmRefusal>
        </div>
      )}
    </div>
  );
}
