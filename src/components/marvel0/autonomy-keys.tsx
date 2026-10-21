"use client";
// The keys of terminal D as small buttons, and the window that stands in front
// of the ones that move the arm. Each button carries the byte it stands for
// (a badge: `Enter`, `y`, `s` ...), what it does in words, and one line under
// it: that the arm moves, how long until it can be clicked, or why it is off.
// Colour carries state only: no motion is neutral, an arm that moves has a
// warning edge AND a line of words AND an icon, never the colour alone.
//
// A level A key (n, q, cancel of a resume) is one click. A level B or C key
// opens a confirm window first: Cancel has the initial focus (a stray Enter
// cancels), Confirm is off until the key's 1.5 s delay and the 3 s gap after
// the last key have passed, and a window whose prompt moved on while it was
// open closes by itself and sends nothing. There is no keyboard shortcut and no
// typed phrase: the page sends the phrase of a level C key itself and the
// backend still checks it. A click is a person's decision; nothing here sends
// a key by itself.
import { useCallback, useEffect, useId, useState } from "react";
import { Footprints, Hand, Lock, TriangleAlert } from "lucide-react";
import { Button, Kbd } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { WordedText } from "./action-parts";
import {
  ConfirmRefusal,
  ConfirmWindow,
  type ConfirmAttest,
} from "./confirm-window";
import { wordOf } from "./ctl-text";
import type { KeyId, KeyView, RunView } from "./autonomy-doc";
import {
  executePhrase,
  groupBySlot,
  homingKey,
  homingStep,
  type KeyVM,
} from "./autonomy-logic";
import type { AutonomyActions } from "./use-autonomy";

/** The label of each key: what it does, in the page's words. */
export const KEY_LABEL: Record<KeyId, string> = {
  execute: "marvel0.autonomy.key.execute",
  execute_variant: "marvel0.autonomy.key.execute_variant",
  inventory_yes: "marvel0.autonomy.key.inventory_yes",
  inventory_no: "marvel0.autonomy.key.inventory_no",
  home_o: "marvel0.autonomy.key.home_o",
  home_p: "marvel0.autonomy.key.home_p",
  home_h: "marvel0.autonomy.key.home_h",
  release_round: "marvel0.autonomy.key.release_round",
  verdict_success: "marvel0.autonomy.key.verdict_success",
  verdict_failure: "marvel0.autonomy.key.verdict_failure",
  verdict_discard: "marvel0.autonomy.key.verdict_discard",
  resume_ask: "marvel0.autonomy.key.resume_ask",
  resume_confirm: "marvel0.autonomy.key.resume_confirm",
  resume_cancel: "marvel0.autonomy.key.resume_cancel",
  quit: "marvel0.autonomy.key.quit",
};

/** The label of a key as shown: `resume_ask` has two sentences, by what D
 * does after the Enter; a document that does not say gets the stricter one
 * (the arm may move at once). */
export function keyLabelKey(key: KeyView): string {
  return key.id === "resume_ask" && key.variant === "confirm_reason"
    ? "marvel0.autonomy.key.resume_ask_confirm"
    : KEY_LABEL[key.id];
}

/** What a key does that its label does not say: the sentences of its window. */
const KEY_HAZARD: Partial<Record<KeyId, string[]>> = {
  execute: ["marvel0.autonomy.hazard.execute"],
  execute_variant: ["marvel0.autonomy.hazard.execute"],
  inventory_yes: ["marvel0.autonomy.hazard.inventory_yes"],
  home_o: ["marvel0.autonomy.hazard.home_o"],
  home_p: ["marvel0.autonomy.hazard.home_p"],
  home_h: ["marvel0.autonomy.hazard.home_h"],
  release_round: ["marvel0.autonomy.hazard.release_round"],
  verdict_success: ["marvel0.autonomy.hazard.verdict"],
  verdict_failure: ["marvel0.autonomy.hazard.verdict"],
  verdict_discard: ["marvel0.autonomy.hazard.discard"],
  resume_ask: ["marvel0.autonomy.hazard.resume_ask"],
  resume_confirm: ["marvel0.autonomy.hazard.resume_confirm"],
};

/** The ticks of a window: what only a person can check. */
const KEY_ATTEST: Partial<Record<KeyId, string[]>> = {
  execute: ["estop", "area_clear"],
  execute_variant: ["estop", "leftovers_checked"],
};

const MOTION_KEY: Record<KeyView["motion"], string | null> = {
  none: null,
  arm_moves: "marvel0.autonomy.motion.arm_moves",
  may_move: "marvel0.autonomy.motion.may_move",
};

const seconds = (ms: number): string => (ms / 1000).toFixed(1);

/** The line under a button: the lock, the countdown or the reason it is off. */
function StatusLine({ vm }: { vm: KeyVM }) {
  const { t } = useLocale();
  if (vm.lock === "sending") return <>{t("marvel0.autonomy.lock.sending")}</>;
  if (vm.lock === "sent") return <>{t("marvel0.autonomy.lock.sent")}</>;
  if (vm.lock === "taken") return <>{t("marvel0.autonomy.lock.taken")}</>;
  if (vm.lock === "unseen") return <>{t("marvel0.autonomy.lock.unseen")}</>;
  if (vm.mode === "wait")
    return <>{t("marvel0.autonomy.wait").replace("{s}", seconds(vm.waitMs))}</>;
  if (vm.mode === "off" && vm.why) return <WordedText worded={vm.why} />;
  return null;
}

export function KeyButton({
  vm,
  onPress,
}: {
  vm: KeyVM;
  onPress: (key: KeyView) => void;
}) {
  const { t } = useLocale();
  const id = useId();
  const { key } = vm;
  const motion = MOTION_KEY[key.motion];
  const clickable = vm.mode === "ready";
  return (
    <div
      className="rg-akey"
      data-action={key.id}
      data-mode={vm.mode}
      data-lock={vm.lock ?? undefined}
      data-motion={key.motion}
      data-slot={key.slot}
      data-level={key.level}
    >
      <Button
        size="lg"
        variant="secondary"
        className={`rg-akey__btn${key.motion === "none" ? "" : " rg-akey__btn--moves"}`}
        disabled={!clickable}
        loading={vm.lock === "sending"}
        aria-describedby={id}
        data-action={key.id}
        onClick={() => onPress(key)}
      >
        <Kbd>{key.byte}</Kbd>
        <span className="rg-akey__label">{t(keyLabelKey(key))}</span>
      </Button>
      <p id={id} className="rg-akey__note">
        {motion && (
          <span
            className="rg-akey__moves"
            data-testid={`marvel0-autonomy-moves-${key.id}`}
          >
            <TriangleAlert size={13} aria-hidden="true" />
            {t(motion)}
          </span>
        )}
        <span className="rg-akey__state" role="status">
          <StatusLine vm={vm} />
        </span>
      </p>
    </div>
  );
}

/** Where a window was opened: the key it is about and the state it was looking
 * at. If the state moves on, the window closes and nothing is sent. */
export interface Asking {
  id: KeyId;
  runId: string | null;
  seq: number;
}

export function KeyConfirm({
  asking,
  run,
  vm,
  actions,
  onClose,
}: {
  asking: Asking | null;
  run: RunView;
  /** The state of the key now (the window follows it); null when the key is
   * no longer in the document. */
  vm: KeyVM | null;
  actions: AutonomyActions;
  onClose: () => void;
}) {
  const { t } = useLocale();
  const moved =
    asking !== null &&
    (run.runId !== asking.runId || run.state.seq !== asking.seq);
  // The prompt moved on while the window was open: close it, send nothing.
  const gone = asking !== null && !moved && vm === null;
  useEffect(() => {
    if (asking && (moved || gone)) onClose();
  }, [asking, moved, gone, onClose]);
  const open = asking !== null && !moved && vm !== null;
  const key = vm?.key ?? null;
  const refused =
    asking && actions.refused?.key === asking.id ? actions.refused : null;
  const label = key ? t(keyLabelKey(key)) : "";
  const attest: ConfirmAttest[] = (key ? (KEY_ATTEST[key.id] ?? []) : []).map(
    (code) => ({ code, label: wordOf(t, "marvel0.autonomy.ack.", code) }),
  );
  const ready = vm?.mode === "ready" && actions.working === null;
  const waiting = vm?.mode === "wait";
  return (
    <ConfirmWindow
      open={open}
      testId="marvel0-autonomy-key-dialog"
      title={label}
      intro={t("marvel0.autonomy.confirm.intro")}
      estop={
        key && key.motion !== "none" ? t("marvel0.autonomy.estop_line") : null
      }
      hazards={[
        ...(key ? (KEY_HAZARD[key.id] ?? []) : []).map((hazard) => t(hazard)),
        ...(key && MOTION_KEY[key.motion] ? [t(MOTION_KEY[key.motion]!)] : []),
      ]}
      notes={[t("marvel0.autonomy.gates_scope")]}
      attest={attest}
      confirmLabel={
        waiting && vm
          ? `${label} (${seconds(vm.waitMs)} s)`
          : label || t("marvel0.autonomy.key.quit")
      }
      confirmIcon={key?.motion !== "none" ? Hand : undefined}
      ready={ready}
      working={actions.working === asking?.id}
      refused={
        refused ? (
          <ConfirmRefusal
            failure={refused.failure}
            testId="marvel0-autonomy-key-refused"
          >
            <WordedText worded={refused.worded} />
          </ConfirmRefusal>
        ) : null
      }
      onCancel={onClose}
      onConfirm={() => {
        if (!key || vm?.mode !== "ready") return;
        const phrase =
          key.level === "C" ? executePhrase(key, run.name) : undefined;
        return actions.pressKey(key, run, phrase).then((ok) => {
          if (ok) onClose();
        });
      }}
    />
  );
}

/** The three homing confirmations in order, so that the person sees where in
 * the three they are: each is its own click with its own window. */
function HomingSteps({ step }: { step: 1 | 2 | 3 }) {
  const { t } = useLocale();
  return (
    <ol
      className="rg-ahome"
      data-testid="marvel0-autonomy-homing"
      data-step={step}
    >
      {([1, 2, 3] as const).map((n) => (
        <li
          key={n}
          aria-current={n === step ? "step" : undefined}
          data-done={n < step || undefined}
        >
          {t(homingKey(n))}
        </li>
      ))}
    </ol>
  );
}

/** The keys offered now, in two groups: the first step, and the step after it,
 * which sits apart so that one hand cannot press both in the same place. */
export function KeysBlock({
  run,
  vms,
  onPress,
}: {
  run: RunView;
  vms: KeyVM[];
  onPress: (key: KeyView) => void;
}) {
  const { t } = useLocale();
  const groups = groupBySlot(vms.map((vm) => vm.key));
  const byId = new Map(vms.map((vm) => [vm.key.id, vm]));
  const render = (keys: KeyView[]) =>
    keys.map((key) => (
      <KeyButton key={key.id} vm={byId.get(key.id)!} onPress={onPress} />
    ));
  const step = homingStep(run.state.id);
  if (vms.length === 0) return null;
  return (
    <div
      className="rg-akeys"
      data-testid="marvel0-autonomy-keys"
      data-state={run.state.id}
    >
      <h4 className="rg-marker__title">{t("marvel0.autonomy.keys.title")}</h4>
      {step !== 0 && <HomingSteps step={step} />}
      <div
        className={`rg-akeys__cols${groups.first.length > 0 && groups.second.length > 0 ? " rg-akeys__cols--two" : ""}`}
      >
        {groups.first.length > 0 && (
          <div className="rg-akeys__col" data-slot="first">
            {render(groups.first)}
          </div>
        )}
        {groups.second.length > 0 && (
          <div
            className="rg-akeys__col rg-akeys__col--second"
            data-slot="second"
          >
            <p className="rg-akeys__second">
              <Footprints size={14} aria-hidden="true" />
              {t("marvel0.autonomy.keys.second")}
            </p>
            {render(groups.second)}
          </div>
        )}
      </div>
      <p className="rg-muted rg-akeys__lock">
        <Lock size={13} aria-hidden="true" />
        {t("marvel0.autonomy.keys.once")}
      </p>
    </div>
  );
}

/** Opens the window of a key, or sends a level A key at once. The state the
 * window was opened in is the state the key is sent in. */
export function useKeyPress(
  run: RunView | null,
  actions: AutonomyActions,
): {
  asking: Asking | null;
  press: (key: KeyView) => void;
  close: () => void;
} {
  const [asking, setAsking] = useState<Asking | null>(null);
  const close = useCallback(() => setAsking(null), []);
  const press = (key: KeyView) => {
    if (!run) return;
    actions.clear();
    if (key.level === "A") {
      void actions.pressKey(key, run);
      return;
    }
    setAsking({ id: key.id, runId: run.runId, seq: run.state.seq });
  };
  return { asking, press, close };
}
