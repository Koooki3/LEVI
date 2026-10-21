"use client";
// "Start the client", under the command the command card made: it starts
// the very client that command describes, hosted by LEVI (run-panel.tsx drives
// it afterwards). A start is one confirm window: what will run, the three
// things only a person can check (the scene is reset, the area is clear, a
// hand is on the physical e-stop) as tick boxes, Cancel (focused) and Start.
// The page sends the confirmation phrase itself; nothing is typed. The copy of
// the command stays as the manual route. The checks stop mistakes, not another
// program of the same account.
import { useState } from "react";
import { Play } from "lucide-react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { WordedText } from "./action-parts";
import { ConfirmRefusal, ConfirmWindow } from "./confirm-window";
import { wordOf } from "./ctl-text";
import { startPhrase, type RunView } from "./run-logic";
import type { RunActions } from "./use-run-actions";

const ACKS = ["estop", "scene_reset", "area_clear"] as const;

export function RunStart({
  view,
  actions,
  form,
  group,
  modelId,
  rtc,
  stale,
}: {
  /** The hosted client's state; null while it is not read. */
  view: RunView | null;
  actions: RunActions;
  /** The form of the command, as the preview request sent it. */
  form: Record<string, unknown>;
  /** The rollout group of this run: the phrase names it. */
  group: string;
  modelId: string;
  rtc: boolean;
  /** The form changed after the command was made: it is made again first. */
  stale: boolean;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const action = view?.actions.start ?? null;
  const phrase = startPhrase(view, group);
  const working = actions.working !== null;
  const refused = actions.refused?.key === "start" ? actions.refused : null;
  const close = () => setOpen(false);
  const off = !action?.allowed || stale || working;
  return (
    <div className="rg-runstart" data-testid="marvel0-run-start">
      <Button
        variant="primary"
        icon={Play}
        disabled={off}
        onClick={() => {
          actions.clear();
          setOpen(true);
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
      <ConfirmWindow
        open={open}
        testId="marvel0-run-start-dialog"
        title={t("marvel0.run.start.title")}
        intro={t("marvel0.run.start.intro")}
        facts={
          <dl className="rg-facts">
            <div className="rg-fact">
              <dt>{t("marvel0.deploy.fact.model")}</dt>
              <dd>
                <code>{modelId}</code>
              </dd>
            </div>
            <div className="rg-fact">
              <dt>{t("marvel0.deploy.fact.rtc")}</dt>
              <dd>
                {t(
                  rtc ? "marvel0.deploy.value.on" : "marvel0.deploy.value.off",
                )}
              </dd>
            </div>
            <div className="rg-fact">
              <dt>{t("marvel0.cmd.field.group")}</dt>
              <dd>
                <code>{group}</code>
              </dd>
            </div>
          </dl>
        }
        estop={t("marvel0.run.start.estop")}
        hazards={[t("marvel0.run.start.first_enter")]}
        notes={[t("marvel0.stack.gates_scope")]}
        attest={ACKS.map((name) => ({
          code: name,
          label: wordOf(t, "marvel0.run.ack.", name),
        }))}
        confirmLabel={t("marvel0.run.start.go")}
        confirmIcon={Play}
        ready={!working}
        working={actions.working === "start"}
        refused={
          refused ? (
            <ConfirmRefusal
              failure={refused.failure}
              testId="marvel0-run-start-refused"
            >
              <WordedText worded={refused.worded} />
            </ConfirmRefusal>
          ) : null
        }
        onCancel={close}
        onConfirm={(ticked) =>
          actions
            .start(form, phrase, {
              estop: ticked.estop === true,
              scene_reset: ticked.scene_reset === true,
              area_clear: ticked.area_clear === true,
            })
            .then((ok) => {
              if (ok) close();
            })
        }
      />
    </div>
  );
}
