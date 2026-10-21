"use client";
// The confirm window of every write of the arm stack: starting or stopping a
// terminal, the gripper test, the marker, the guided start and the stop of the
// whole stack. It states what the action does to the machine, asks the checks
// only a person can make (ticks) and has a Cancel with the initial focus and a
// Confirm button. What it says comes from stack-dialog-logic.ts; it sends
// nothing itself (the caller's `onConfirm` does), and the confirmation phrase
// is the spec's, sent by the page: it is not typed. The only text a person
// types is the override of a stop of C with a client connected.
import { useState } from "react";
import { Field, Input, Switch } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { WordedText } from "./action-parts";
import { ConfirmRefusal, ConfirmWindow } from "./confirm-window";
import type { ConfirmInput, Refused } from "./use-stack-actions";
import { overrideReady, type DialogSpec } from "./stack-dialog-logic";
import { sayText } from "./marvel0-say";

function Window({
  spec,
  working,
  refused,
  onClose,
  onConfirm,
}: {
  spec: DialogSpec;
  working: boolean;
  refused: Refused | null;
  onClose: () => void;
  onConfirm: (input: ConfirmInput) => unknown;
}) {
  const { t } = useLocale();
  const [override, setOverride] = useState("");
  // The whole stack's start runs through to C on this one confirmation; the
  // person may ask it to pause before B and before C. A choice, not a check:
  // off at every opening.
  const [pause, setPause] = useState(false);
  // The override phrase is asked for when the stop is blocked by a client on
  // the robot server now, or the backend just refused it for that.
  const needOverride =
    spec.override !== null &&
    (spec.override.now || refused?.failure.code === "client_connected");
  return (
    <ConfirmWindow
      open
      testId="marvel0-ctl-dialog"
      title={sayText(spec.title, t)}
      intro={spec.intro ? sayText(spec.intro, t) : undefined}
      estop={spec.estop ? t("marvel0.ctl.estop") : null}
      hazards={spec.hazards.map((hazard) => sayText(hazard, t))}
      notes={[t("marvel0.stack.gates_scope"), t("marvel0.ctl.unverified")]}
      attest={spec.attest.map((item) => ({
        code: item.code,
        label: sayText(item.label, t),
      }))}
      // The choice comes first, above the long hazard text: below it, in a
      // window taller than the screen, it would be out of sight.
      facts={
        spec.pauseOption ? (
          <Switch
            checked={pause}
            onChange={(event) => setPause(event.target.checked)}
            label={t("marvel0.ctl.dialog.sequence_pause")}
            description={t("marvel0.ctl.dialog.sequence_pause_hint")}
          />
        ) : undefined
      }
      after={
        spec.override && needOverride ? (
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
        ) : null
      }
      ready={overrideReady(spec, override, needOverride)}
      confirmLabel={t(spec.confirmKey)}
      danger={spec.danger}
      working={working}
      refused={
        refused ? (
          <ConfirmRefusal
            failure={refused.failure}
            testId="marvel0-ctl-dialog-refused"
          >
            <WordedText worded={refused.worded} />
          </ConfirmRefusal>
        ) : null
      }
      onCancel={onClose}
      onConfirm={() => onConfirm({ override, pause })}
    />
  );
}

export function StackDialog({
  spec,
  working,
  refused,
  onClose,
  onConfirm,
}: {
  /** What to ask; null keeps the window closed. */
  spec: DialogSpec | null;
  /** The write is on its way (the buttons wait). */
  working: boolean;
  /** The refusal of the write this window sent, shown inside it. */
  refused: Refused | null;
  onClose: () => void;
  onConfirm: (input: ConfirmInput) => unknown;
}) {
  if (!spec) return null;
  // What was typed or ticked belongs to one opening of one window.
  return (
    <Window
      key={`${spec.confirmKey}:${spec.phrase}`}
      spec={spec}
      working={working}
      refused={refused}
      onClose={onClose}
      onConfirm={onConfirm}
    />
  );
}
