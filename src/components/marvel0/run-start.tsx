"use client";
// "Start the client", under the command the command card made: it starts
// the very client that command describes, hosted by LEVI (run-panel.tsx drives
// it afterwards). A start is one dialog: what will run, the three things only
// a person can check (the scene is reset, the area is clear, a hand is on the
// physical e-stop) and the phrase to type. The copy of the command stays as
// the manual route. The checks and the phrase stop mistakes, not another
// program of the same account.
import { useState } from "react";
import { Play } from "lucide-react";
import { Button, Checkbox, Dialog, Field, Input } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { WordedText } from "./action-parts";
import { wordOf } from "./ctl-text";
import { sayText } from "./marvel0-say";
import { startPhrase, type RunView } from "./run-logic";
import type { RunActions } from "./use-run-actions";

const ACKS = ["estop", "scene_reset", "area_clear"] as const;
type AckName = (typeof ACKS)[number];

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
  const [typed, setTyped] = useState("");
  const [ticked, setTicked] = useState<Record<AckName, boolean>>({
    estop: false,
    scene_reset: false,
    area_clear: false,
  });
  const action = view?.actions.start ?? null;
  const phrase = startPhrase(view, group);
  const working = actions.working !== null;
  const refused = actions.refused?.key === "start" ? actions.refused : null;
  const ready =
    typed.trim() === phrase && ACKS.every((name) => ticked[name]) && !working;
  const close = () => {
    setOpen(false);
    setTyped("");
    setTicked({ estop: false, scene_reset: false, area_clear: false });
  };
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
      <Dialog
        open={open}
        onClose={() => {
          if (!working) close();
        }}
        title={t("marvel0.run.start.title")}
        description={t("marvel0.run.start.intro")}
        footer={
          <>
            <Button variant="secondary" disabled={working} onClick={close}>
              {t("marvel0.ctl.btn.cancel")}
            </Button>
            <Button
              variant="primary"
              icon={Play}
              loading={actions.working === "start"}
              disabled={!ready}
              onClick={() =>
                void actions
                  .start(form, phrase, {
                    estop: ticked.estop,
                    scene_reset: ticked.scene_reset,
                    area_clear: ticked.area_clear,
                  })
                  .then((ok) => {
                    if (ok) close();
                  })
              }
            >
              {t("marvel0.run.start.go")}
            </Button>
          </>
        }
      >
        <div className="rg-dialog" data-testid="marvel0-run-start-dialog">
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
          <Note tone="warning" role="alert">
            {t("marvel0.run.start.estop")}
          </Note>
          <p className="rg-muted">{t("marvel0.run.start.first_enter")}</p>
          <p className="rg-muted">{t("marvel0.stack.gates_scope")}</p>
          {ACKS.map((name) => (
            <Checkbox
              key={name}
              checked={ticked[name]}
              onChange={(event) =>
                setTicked((now) => ({ ...now, [name]: event.target.checked }))
              }
              label={wordOf(t, "marvel0.run.ack.", name)}
            />
          ))}
          <Field
            label={t("marvel0.run.phrase")}
            hint={sayText(
              { key: "marvel0.run.phrase_hint", params: { phrase } },
              t,
            )}
          >
            <Input
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </Field>
          {refused && (
            <p
              className="rg-bad"
              role="alert"
              data-testid="marvel0-run-start-refused"
              data-code={refused.failure.code}
            >
              <WordedText worded={refused.worded} />
            </p>
          )}
        </div>
      </Dialog>
    </div>
  );
}
