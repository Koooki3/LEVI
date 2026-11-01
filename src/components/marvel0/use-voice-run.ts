"use client";
// The voice run of the action page (voice-run.ts has the rules): whether it is
// on and with which ticks, what the last spoken sentence did, and the one
// Enter it presses at the start gate of a client it started. The page owns it
// (it sees the hosted client and the instruction change); the voice card
// switches it and shows its state, the command card makes the command.
//
// Every turn of the switch has a generation number. A start or change still
// on its way when the switch goes off (or a frozen client turns it off) finds
// another generation when it comes back and leaves no wait behind, and the
// wait itself presses Enter only for its own generation: switching off right
// after a wrong sentence always stops the episode from starting by itself.
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
  /** A hold was sent while waiting for the gate: Enter is not pressed. */
  | "held"
  /** Switched off by itself: the client was frozen or lost. */
  | "off_held";

export interface VoiceRunStatus {
  phase: VoiceRunPhase;
  prompt: string;
  why: Worded<Say> | null;
  /** A refused sentence also dropped the Enter an earlier one waited for. */
  dropped?: boolean;
}

export interface VoiceRun {
  on: boolean;
  /** When the switch was turned on (epoch ms); 0 while off. */
  armedAt: number;
  status: VoiceRunStatus | null;
  arm: (attest: ChangeAttest) => void;
  disarm: () => void;
  /** Runs one sentence checked against `modelId`'s list. `start` makes the
   * command for it and starts the client with the ticks (the command card
   * does that: it has the form). */
  submit: (
    prompt: string,
    modelId: string,
    start: (ack: Ack) => Promise<boolean>,
  ) => Promise<void>;
}

const OFF_STATES = new Set(["held", "lost"]);

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
  const [armed, setArmed] = useState<{
    attest: ChangeAttest;
    at: number;
  } | null>(null);
  const [status, setStatus] = useState<VoiceRunStatus | null>(null);
  const [wait, setWait] = useState<GateWait | null>(null);
  const [tick, setTick] = useState(0);
  const armedRef = useRef(armed);
  armedRef.current = armed;
  /** Bumped by every arm, disarm and automatic switch-off. */
  const generation = useRef(0);
  const busy = useRef(false);
  /** The run Enter was pressed for: never twice for one run. */
  const entered = useRef<string | null>(null);
  const view = run?.value ?? null;
  const current = run?.current ?? false;
  const viewRef = useRef({ view, current });
  viewRef.current = { view, current };
  const waitRef = useRef(wait);
  waitRef.current = wait;

  const off = useCallback((next: VoiceRunStatus | null) => {
    generation.current += 1;
    setArmed(null);
    setWait(null);
    setStatus(next);
  }, []);
  const disarm = useCallback(() => off(null), [off]);
  const arm = useCallback((ticks: ChangeAttest) => {
    if (!ticks.estop || !ticks.scene_reset || !ticks.area_clear) return;
    // A frozen or lost client is ended first; the ticks are for a scene after it.
    const state = viewRef.current.view?.state;
    if (state && OFF_STATES.has(state)) {
      setStatus({ phase: "off_held", prompt: "", why: null });
      return;
    }
    generation.current += 1;
    setArmed({ attest: { ...ticks }, at: Date.now() });
    setWait(null);
    setStatus(null);
  }, []);

  const submit = useCallback(
    async (
      prompt: string,
      modelId: string,
      start: (ack: Ack) => Promise<boolean>,
    ) => {
      const ticks = armedRef.current?.attest;
      if (!ticks || !runActions || busy.current) return;
      const mine = generation.current;
      const still = () => generation.current === mine;
      const { view: now, current: fresh } = viewRef.current;
      const route = voiceRoute(
        now,
        fresh,
        runActions.working !== null || runActions.holding,
        prompt,
        modelId,
      );
      const say = (phase: VoiceRunPhase, why: Worded<Say> | null = null) =>
        setStatus({ phase, prompt, why });
      if (route.kind === "refuse") {
        // A newer sentence means the person changed their mind: an Enter
        // still waited for an earlier one is not pressed either.
        const earlier = waitRef.current;
        const dropped = earlier !== null && earlier.prompt !== prompt;
        if (dropped) setWait(null);
        setStatus({ phase: "refused", prompt, why: route.why, dropped });
        return;
      }
      busy.current = true;
      setWait(null);
      try {
        if (route.kind === "enter") {
          say("entering");
          entered.current = now?.run?.runId ?? null;
          const ok = await runActions.press("enter");
          say(ok ? "entered" : "enter_failed");
          return;
        }
        const until = Date.now() + GATE_WAIT_MS;
        if (route.kind === "change") {
          say("changing");
          const sent = await changeInstruction(prompt, ticks);
          if (!still()) return;
          if (sent)
            setWait({
              route: "change",
              generation: mine,
              prompt,
              afterRunId: route.runId,
              until,
            });
          else say("start_failed");
          return;
        }
        say("starting");
        const started = await start(ticks);
        if (!still()) return;
        if (started) {
          say("waiting");
          setWait({
            route: "start",
            generation: mine,
            prompt,
            afterRunId: route.afterRunId,
            until,
          });
        } else say("start_failed");
      } finally {
        busy.current = false;
      }
    },
    [runActions, changeInstruction],
  );

  // An instruction change whose new client the page will not start is over
  // (an "unknown" end may still start it within the page's minute: wait on).
  useEffect(() => {
    if (wait?.route !== "change") return;
    if (
      changeState === "refused" ||
      changeState === "failed" ||
      changeState === "ready"
    ) {
      setWait(null);
      setStatus({ phase: "start_failed", prompt: wait.prompt, why: null });
    }
  }, [wait, changeState]);

  // Enter at the first start gate of the client the voice started, once.
  const holding = runActions?.holding ?? false;
  useEffect(() => {
    if (!wait || !runActions) return;
    if (!armedRef.current || wait.generation !== generation.current) {
      setWait(null);
      return;
    }
    if (holding) {
      setWait(null);
      setStatus({ phase: "held", prompt: wait.prompt, why: null });
      return;
    }
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
  }, [wait, view, current, runActions, holding, tick]);

  // A frozen or lost client switches the voice run off: the ticks were for
  // the scene before it.
  const state = view?.state;
  useEffect(() => {
    if (!armedRef.current || !state || !OFF_STATES.has(state)) return;
    off({ phase: "off_held", prompt: "", why: null });
  }, [state, off]);

  return {
    on: armed !== null,
    armedAt: armed?.at ?? 0,
    status,
    arm,
    disarm,
    submit,
  };
}
