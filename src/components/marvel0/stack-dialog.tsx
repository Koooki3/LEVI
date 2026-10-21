"use client";
// The confirmation dialog of every write of the arm stack: starting or
// stopping a terminal, the gripper test, the marker, the guided sequence. It
// follows the policy server's dialog: the fixed sentence about the physical
// e-stop, what the action does to the machine, the checks only a person can
// make, and the phrase to type. What it says comes from stack-dialog-logic.ts;
// it sends nothing itself (the caller's `onConfirm` does).
import { useEffect, useState } from "react";
import { Button, Checkbox, Dialog, Field, Input } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { WordedText } from "./action-parts";
import type { Refused } from "./use-stack-actions";
import { dialogReady, type DialogSpec } from "./stack-dialog-logic";
import { sayText } from "./marvel0-say";

export function StackDialog({
  spec,
  working,
  refused,
  onClose,
  onConfirm,
}: {
  /** What to ask; null keeps the dialog closed. */
  spec: DialogSpec | null;
  /** The write is on its way (the buttons wait). */
  working: boolean;
  /** The refusal of the write this dialog sent, shown inside it. */
  refused: Refused | null;
  onClose: () => void;
  onConfirm: (typed: string, override: string) => void;
}) {
  const { t } = useLocale();
  const [typed, setTyped] = useState("");
  const [override, setOverride] = useState("");
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  // What was typed or ticked belongs to one opening of one dialog.
  useEffect(() => {
    setTyped("");
    setOverride("");
    setTicked({});
  }, [spec?.phrase, spec?.confirmKey]);
  if (!spec) return null;
  // The override phrase is asked for when the stop is blocked by a client on
  // the robot server now, or the backend just refused it for that.
  const needOverride =
    spec.override !== null &&
    (spec.override.now || refused?.failure.code === "client_connected");
  const ready = dialogReady(spec, typed, ticked, override, needOverride);
  return (
    <Dialog
      open
      onClose={() => {
        if (!working) onClose();
      }}
      title={sayText(spec.title, t)}
      description={spec.intro ? sayText(spec.intro, t) : undefined}
      footer={
        <>
          <Button variant="secondary" disabled={working} onClick={onClose}>
            {t("marvel0.ctl.btn.cancel")}
          </Button>
          <Button
            variant={spec.danger ? "danger" : "primary"}
            loading={working}
            disabled={!ready || working}
            onClick={() => onConfirm(typed, override)}
          >
            {t(spec.confirmKey)}
          </Button>
        </>
      }
    >
      <div data-testid="marvel0-ctl-dialog" className="rg-dialog">
        {spec.estop && (
          <Note tone="warning" role="alert">
            {t("marvel0.ctl.estop")}
          </Note>
        )}
        {spec.hazards.length > 0 && (
          <ul className="rg-notes" data-testid="marvel0-ctl-hazards">
            {spec.hazards.map((hazard, index) => (
              <li key={index}>{sayText(hazard, t)}</li>
            ))}
          </ul>
        )}
        <p className="rg-muted">{t("marvel0.stack.gates_scope")}</p>
        <p className="rg-muted">{t("marvel0.ctl.unverified")}</p>
        {spec.attest.map((item) => (
          <Checkbox
            key={item.code}
            checked={ticked[item.code] === true}
            onChange={(event) =>
              setTicked((now) => ({
                ...now,
                [item.code]: event.target.checked,
              }))
            }
            label={sayText(item.label, t)}
          />
        ))}
        {spec.typed ? (
          <Field
            label={t("marvel0.ctl.dialog.phrase")}
            hint={sayText(
              {
                key: "marvel0.ctl.dialog.phrase_hint",
                params: { phrase: spec.phrase },
              },
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
        ) : null}
        {spec.override && needOverride && (
          <>
            <Note tone="warning" role="alert">
              {t("marvel0.ctl.dialog.client_on_server")}
            </Note>
            <Field
              label={t("marvel0.ctl.dialog.override")}
              hint={sayText(
                {
                  key: "marvel0.ctl.dialog.phrase_hint",
                  params: { phrase: spec.override.phrase },
                },
                t,
              )}
            >
              <Input
                value={override}
                onChange={(event) => setOverride(event.target.value)}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
              />
            </Field>
          </>
        )}
        {refused && (
          <p
            className="rg-bad"
            role="alert"
            data-testid="marvel0-ctl-dialog-refused"
            data-code={refused.failure.code}
          >
            <WordedText worded={refused.worded} />
          </p>
        )}
      </div>
    </Dialog>
  );
}
