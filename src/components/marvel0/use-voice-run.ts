"use client";
// The voice run of the action page (voice-run.ts has the rules): whether it is
// on and with which ticks, what the last spoken sentence did, and the one
// Enter it presses at the start gate of a client it started. The page owns it
// (it sees the hosted client and the instruction change); the voice card
// switches it and shows its state, the command card makes the command.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import type { ChangeAttest, EpisodeChangeStatus } from "./run-panel";
import type { Ack, RunActions } from "./use-run-actions";
import type { RunPoll } from "./use-run-poll";
import { GATE_WAIT_MS, gateStep, voiceRoute, type GateWait } from "./voice-run";

export type VoiceRunPhase =
  /** Making the command and starting the client. */
  | "starting"
  /** Ending the client at its gate to start it with the new instruction. */
  | "changing"
  /** The client was started; Enter follows at its start gate. */
  | "waiting"
  | "entering"
  | "entered"
  | "refused"
  | "start_failed"
  | "enter_failed"
  | "late"
  | "gone"
  /** Switched off by itself: the client was frozen or lost. */
  | "off_held";

export interface VoiceRunStatus {
  phase: VoiceRunPhase;
  prompt: string;
  why: Worded<Say> | null;
}

export interface VoiceRun {
  on: boolean;
  status: VoiceRunStatus | null;
  arm: (attest: ChangeAttest) => void;
  disarm: () => void;
  /** Runs one sentence. `start` makes the command for it and starts the
   * client with the ticks (the command card does that: it has the form). */
  submit: (
    prompt: string,
    start: (ack: Ack) => Promise<boolean>,
  ) => Promise<void>;
}

export function useVoiceRun({
  run,
  runActions,
  changeInstruction,
  changeState,
}: {
  run?: RunPoll;
  runActions?: RunActions;
  /** The page's instruction change at the gate; true when the end was sent. */
  changeInstruction: (prompt: string, attest: ChangeAttest) => Promise<boolean>;
  changeState: EpisodeChangeStatus["state"];
}): VoiceRun {
  const [attest, setAttest] = useState<ChangeAttest | null>(null);
  const [status, setStatus] = useState<VoiceRunStatus | null>(null);
  const [wait, setWait] = useState<GateWait | null>(null);
  const [tick, setTick] = useState(0);
  const attestRef = useRef(attest);
  attestRef.current = attest;
  const busy = useRef(false);
  /** The run Enter was pressed for: never twice for one run. */
  const entered = useRef<string | null>(null);
  const view = run?.value ?? null;
  const current = run?.current ?? false;
  const viewRef = useRef({ view, current });
  viewRef.current = { view, current };

  const disarm = useCallback(() => {
    setAttest(null);
    setWait(null);
    setStatus(null);
  }, []);
  const arm = useCallback((ticks: ChangeAttest) => {
    if (!ticks.estop || !ticks.scene_reset || !ticks.area_clear) return;
    setAttest({ ...ticks });
    setStatus(null);
  }, []);

  const submit = useCallback(
    async (prompt: string, start: (ack: Ack) => Promise<boolean>) => {
      const ticks = attestRef.current;
      if (!ticks || !runActions || busy.current) return;
      const { view: now, current: fresh } = viewRef.current;
      const route = voiceRoute(now, fresh, runActions.working !== null, prompt);
      const say = (phase: VoiceRunPhase, why: Worded<Say> | null = null) =>
        setStatus({ phase, prompt, why });
      if (route.kind === "refuse") return say("refused", route.why);
      busy.current = true;
      setWait(null);
      try {
        if (route.kind === "enter") {
          say("entering");
          entered.current = now?.run?.runId ?? null;
          say((await runActions.press("enter")) ? "entered" : "enter_failed");
          return;
        }
        const until = Date.now() + GATE_WAIT_MS;
        if (route.kind === "change") {
          say("changing");
          if (await changeInstruction(prompt, ticks))
            setWait({ prompt, afterRunId: route.runId, until });
          else say("start_failed");
          return;
        }
        say("starting");
        if (await start(ticks)) {
          say("waiting");
          setWait({ prompt, afterRunId: route.afterRunId, until });
        } else say("start_failed");
      } finally {
        busy.current = false;
      }
    },
    [runActions, changeInstruction],
  );

  // An instruction change that did not get its new client started is over.
  useEffect(() => {
    if (!wait) return;
    if (
      changeState === "refused" ||
      changeState === "failed" ||
      changeState === "unknown" ||
      changeState === "ready"
    ) {
      setWait(null);
      setStatus({ phase: "start_failed", prompt: wait.prompt, why: null });
    }
  }, [wait, changeState]);

  // Enter at the start gate of the client the voice started, once.
  useEffect(() => {
    if (!wait || !runActions) return;
    const step = gateStep(wait, view, current, Date.now());
    if (step === "wait") {
      const timer = setTimeout(
        () => setTick((n) => n + 1),
        Math.max(0, wait.until - Date.now()),
      );
      return () => clearTimeout(timer);
    }
    // Another write in flight: this runs again when it settles.
    if (step === "enter" && runActions.working !== null) return;
    setWait(null);
    if (step !== "enter")
      return setStatus({ phase: step, prompt: wait.prompt, why: null });
    const runId = view?.run?.runId ?? null;
    if (runId === null || entered.current === runId) return;
    entered.current = runId;
    setStatus({ phase: "entering", prompt: wait.prompt, why: null });
    void runActions.press("enter").then((ok) =>
      setStatus({
        phase: ok ? "entered" : "enter_failed",
        prompt: wait.prompt,
        why: null,
      }),
    );
  }, [wait, view, current, runActions, tick]);

  // A frozen or lost client switches the voice run off: the ticks were for
  // the scene before it.
  const state = view?.state;
  useEffect(() => {
    if (!attestRef.current || (state !== "held" && state !== "lost")) return;
    setAttest(null);
    setWait(null);
    setStatus({ phase: "off_held", prompt: "", why: null });
  }, [state]);

  return { on: attest !== null, status, arm, disarm, submit };
}
