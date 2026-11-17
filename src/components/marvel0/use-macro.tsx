"use client";
// The one-click sequences of Step 5 as buttons (the rules are in
// autonomy-macro.ts): the hook that sends the next key when, and only when,
// `macroMove` says so, and the two small views (the status of a sequence under
// way, and the note a stopped one leaves). A click starts a sequence and is the
// person's word for all of it; the keys still go through `actions.pressKey`.
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Notice } from "./notice";
import type { RunView } from "./autonomy-doc";
import {
  MACRO_STEPS,
  macroEntry,
  macroMove,
  type MacroEnd,
  type MacroId,
  type MacroRun,
} from "./autonomy-macro";
import { keyPhrase, type KeyVM, type Limits } from "./autonomy-logic";
import type { AutonomyActions } from "./use-autonomy";

/** How long the click on Start waits for the D it started to show up alive. */
const ARMED_MS = 60_000;

export interface MacroControl {
  /** The sequence under way (null: none). */
  active: { id: MacroId; sent: number; total: number } | null;
  /** Why the last sequence stopped before its end, until the next one starts or
   * the run changes. */
  ended: { id: MacroId; why: MacroEnd; sent: number; total: number } | null;
  start: (id: MacroId) => void;
  cancel: () => void;
}

export function useMacro({
  run,
  vms,
  actions,
  limits,
}: {
  run: RunView | null;
  vms: readonly KeyVM[];
  actions: AutonomyActions;
  limits: Limits;
}): MacroControl {
  const ref = useRef<MacroRun | null>(null);
  // `ref` is the truth (a second render must never find the key still unsent);
  // the state only makes the page follow it.
  const [, setTick] = useState(0);
  const [ended, setEnded] = useState<MacroControl["ended"]>(null);
  const bump = useCallback(() => setTick((n) => n + 1), []);

  const stop = useCallback(
    (why: MacroEnd) => {
      const m = ref.current;
      if (!m) return;
      ref.current = null;
      setEnded({
        id: m.id,
        why,
        // a refused key was not sent
        sent: m.next - m.from - (why === "refused" ? 1 : 0),
        total: MACRO_STEPS[m.id].length - m.from,
      });
      bump();
    },
    [bump],
  );

  const start = useCallback(
    (id: MacroId) => {
      if (ref.current || !run || !run.runId) return;
      const at = macroEntry(id, run);
      if (at === null) return;
      actions.clear();
      const m: MacroRun = {
        id,
        runId: run.runId,
        from: at,
        next: at,
        sentPrompt: null,
        since: Date.now(),
      };
      ref.current = m;
      setEnded(null);
      bump();
    },
    [run, actions, bump],
  );

  const cancel = useCallback(() => stop("cancelled"), [stop]);

  // After every render (a new document, a new button state): at most one key.
  useEffect(() => {
    const m = ref.current;
    if (!m) return;
    const move = macroMove(
      m,
      run,
      vms,
      Date.now(),
      actions.working !== null,
      limits,
    );
    if (move.do === "wait") return;
    if (move.do === "done") {
      ref.current = null;
      bump();
      return;
    }
    if (move.do === "abort") {
      stop(move.why);
      return;
    }
    const prompt = run?.state.prompt?.id ?? null;
    const next: MacroRun = {
      ...m,
      next: m.next + 1,
      sentPrompt: prompt,
      since: Date.now(),
    };
    ref.current = next;
    bump();
    void actions
      .pressKey(
        move.vm.key,
        run as RunView,
        // a level C key (the start of the run) carries the document's phrase
        keyPhrase(move.vm.key) ?? undefined,
        move.attest,
      )
      .then((ok) => {
        // Refused (the page shows the backend's words) or lost: nothing more.
        if (!ok && ref.current === next) stop("refused");
      });
  });

  // The click on Start is the word for D's start prompt too: once the run it
  // started is alive, the sequence that answers that prompt begins. It waits
  // for D's prompt like the others (D launches, starts its services, asks) and
  // ends on any other screen; an arming that finds no D within a minute lapses.
  const armed = actions.armedStart;
  const clearArmed = actions.clearArmedStart;
  const runName = run?.name ?? null;
  const alive = run?.d.alive === true;
  useEffect(() => {
    if (!armed) return;
    if (Date.now() - armed.at > ARMED_MS) {
      clearArmed();
      return;
    }
    if (ref.current || !run || !run.runId || !alive || runName !== armed.name)
      return;
    clearArmed();
    ref.current = {
      id: "start",
      runId: run.runId,
      from: 0,
      next: 0,
      sentPrompt: null,
      since: Date.now(),
    };
    setEnded(null);
    bump();
  }, [armed, clearArmed, run, alive, runName, bump]);

  // Another run (or none): a note about the old one is not shown.
  const runId = run?.runId ?? null;
  useEffect(() => {
    setEnded((now) => (now ? null : now));
  }, [runId]);

  const m = ref.current;
  return {
    active: m
      ? {
          id: m.id,
          sent: m.next - m.from,
          total: MACRO_STEPS[m.id].length - m.from,
        }
      : null,
    ended,
    start,
    cancel,
  };
}

const ENDED_TEXT: Record<MacroEnd, string> = {
  unexpected_screen: "marvel0.autonomy.macro.ended.unexpected_screen",
  timeout: "marvel0.autonomy.macro.ended.timeout",
  run_changed: "marvel0.autonomy.macro.ended.run_changed",
  refused: "marvel0.autonomy.macro.ended.refused",
  cancelled: "marvel0.autonomy.macro.ended.cancelled",
};

/** The status of a sequence under way, with the way to stop sending the rest,
 * and the note a stopped one leaves. Nothing here stops D. */
export function MacroStatus({ macro }: { macro: MacroControl }) {
  const { t } = useLocale();
  if (macro.active)
    return (
      <div
        className="rg-amacro"
        role="status"
        data-testid="marvel0-autonomy-macro"
        data-macro={macro.active.id}
        data-sent={macro.active.sent}
      >
        <p>
          {t("marvel0.autonomy.macro.running")
            .replace("{sent}", String(macro.active.sent))
            .replace("{total}", String(macro.active.total))}
        </p>
        <Button
          size="sm"
          variant="secondary"
          onClick={macro.cancel}
          data-testid="marvel0-autonomy-macro-cancel"
        >
          {t("marvel0.autonomy.macro.cancel")}
        </Button>
        <p className="rg-muted">{t("marvel0.autonomy.macro.cancel_hint")}</p>
      </div>
    );
  if (macro.ended)
    return (
      <Notice
        tone="warning"
        role="status"
        testId="marvel0-autonomy-macro-ended"
      >
        {t(ENDED_TEXT[macro.ended.why])
          .replace("{sent}", String(macro.ended.sent))
          .replace("{total}", String(macro.ended.total))}
      </Notice>
    );
  return null;
}
