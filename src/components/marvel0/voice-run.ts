// Voice run: the switch of the voice card that lets a spoken instruction go
// all the way to a moving arm without the presses in between (owner request
// 2026-11-01: "按这个按钮后，按麦克风说的话会直接输入给模型开跑，而不用人工确认").
//
// Off (the default, and after every page load) the voice card only proposes,
// as before. Switching it on opens one confirm window with the three ticks of
// the start window (hand on the e-stop, scene reset, area clear); those ticks
// are then sent with every start the voice makes until it is switched off.
// While it is on:
//   * a proposal shown after the switch was turned on, with one clear
//     sentence (`autoPick`), is used by itself after VOICE_RUN_DELAY_MS on
//     screen unless the person presses Cancel (which also revokes a use
//     already being checked), opens the other choices or edits the text; any
//     other proposal waits for a Use press, and that press runs the sentence;
//   * the sentence goes where the hosted client is (`voiceRoute`): no client,
//     make the command and start one; at the start gate with the same
//     instruction, press Enter; at the gate with another instruction, end the
//     client there and start it again with the new one (the page's existing
//     instruction change); anything else (an episode running or waiting for
//     its label, a client starting) is refused, never queued;
//   * after a start, Enter is pressed once when that new client reaches its
//     first start gate with that instruction (`gateStep`), within
//     GATE_WAIT_MS, while the switch is still on and no hold is on its way.
//     Switching off (or a frozen client) drops a start still in flight.
// The push-to-talk button is still the only way in: speech near the page does
// nothing unless a person holds the button. Every check of the backend (the
// start's preflight, the challenge of each key) still applies. Pure: no React.
import { commandFormFromRun } from "./command-logic";
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import { mayMove, runStateKey, type RunView } from "./run-logic";
import { effectiveTier, generatedPrompts, type Proposal } from "./speech-logic";

/** How long a clear proposal is on screen before it runs by itself. */
export const VOICE_RUN_DELAY_MS = 2_000;
/** How long a started client has to reach its start gate for Enter. */
export const GATE_WAIT_MS = 120_000;

/** Why a proposal does not run by itself (a Use press still runs it). */
export type NoAutoWhy =
  | "vetoed"
  | "edited"
  | "truncated"
  | "guessed"
  | "disagree"
  | "choose"
  | "person"
  | "earlier"
  | "translated";

export type AutoPick = { prompt: string } | { why: NoAutoWhy };

/** The one sentence a proposal runs by itself, or why it waits for a person.
 * It is always a sentence a Use press could hand over (`canUse`): the tier-A
 * instruction, or the generated sentence (its dataset original when there is
 * one: the policy was trained on that wording). The generated sentence also
 * runs for a tier-B or C answer: B and C mean no allowed instruction says what
 * was heard, while the generated sentence was read back to hold exactly the
 * heard objects with their colours and sizes; but not when one of its names
 * was translated by the local model. `armedAt` is when the switch was turned
 * on: words heard before it never run by themselves, even when a model switch
 * shows them again. */
export function autoPick(proposal: Proposal, armedAt: number): AutoPick {
  const match = proposal.match;
  if (effectiveTier(match) === "V") return { why: "vetoed" };
  if ((proposal.heardAt ?? proposal.shownAt) < armedAt)
    return { why: "earlier" };
  if (proposal.edited) return { why: "edited" };
  if (proposal.truncated) return { why: "truncated" };
  if (proposal.showAll || proposal.editing || proposal.picked !== null)
    return { why: "person" };
  const best = match.tier === "A" && match.best ? match.best.prompt : null;
  const generated = generatedPrompts(match);
  if (generated.length > 0 && match.generated) {
    if (match.generated.repairs.length > 0) return { why: "guessed" };
    if (best !== null && !generated.includes(best)) return { why: "disagree" };
    // A name the local model translated is one the policy was never trained
    // on, and this path has no measured error rate: a person presses Run
    // (independent review 2026-11-01; the owner may choose otherwise).
    if (
      best === null &&
      match.generated.slots.some((slot) => slot.source === "translation")
    )
      return { why: "translated" };
    return {
      prompt: best ?? match.generated.datasetOriginal ?? generated[0],
    };
  }
  if (best !== null) return { prompt: best };
  return { why: "choose" };
}

export type VoiceRoute =
  | { kind: "enter" }
  | { kind: "change"; runId: string }
  | { kind: "start"; afterRunId: string | null }
  | { kind: "refuse"; why: Worded<Say> };

const said = (key: string, params?: Record<string, Say>): Worded<Say> => ({
  say: params ? { key, params } : { key },
  text: "",
});

/** What a sentence does to the hosted client as the page sees it now. */
export function voiceRoute(
  view: RunView | null,
  current: boolean,
  working: boolean,
  prompt: string,
  /** The model of step 3's form: the sentence was checked against its list. */
  modelId: string,
): VoiceRoute {
  if (!view || !current) return { kind: "refuse", why: said(WHY.unread) };
  if (working) return { kind: "refuse", why: said(WHY.busy) };
  const record = view.run;
  if (view.state === "at_gate") {
    if (record?.form?.prompt === prompt) {
      const enter = view.actions.enter;
      return enter.allowed
        ? { kind: "enter" }
        : { kind: "refuse", why: enter.whyNot ?? said(WHY.gate) };
    }
    const end = view.actions.end_at_gate;
    const saved = commandFormFromRun(record?.form);
    // The change restarts the client with its saved form, so its model must
    // be the one the sentence was checked for.
    if (saved && saved.modelId !== modelId)
      return { kind: "refuse", why: said(WHY.model) };
    if (end.allowed && record?.runId && saved)
      return { kind: "change", runId: record.runId };
    return { kind: "refuse", why: end.whyNot ?? said(WHY.gate) };
  }
  if (view.actions.start.allowed)
    return { kind: "start", afterRunId: record?.runId ?? null };
  if (mayMove(view.state))
    return {
      kind: "refuse",
      why: said(WHY.live, { state: { key: runStateKey(view.state) } }),
    };
  return {
    kind: "refuse",
    why: view.actions.start.whyNot ?? said(WHY.cannot_start),
  };
}

/** The reasons of `voiceRoute` that are the voice card's own words. */
export const WHY = {
  unread: "marvel0.speech.run.why.unread",
  busy: "marvel0.speech.run.why.busy",
  gate: "marvel0.speech.run.why.gate",
  live: "marvel0.speech.run.why.live",
  cannot_start: "marvel0.speech.run.why.cannot_start",
  model: "marvel0.speech.run.why.model",
} as const;

/** A start the voice made, waiting for its client's start gate. */
export interface GateWait {
  /** How the client is being started: an instruction change can still fail
   * on the page's side (`changeState`), a start cannot. */
  route: "start" | "change";
  /** The switch's turn it belongs to: switching off or on again ends it. */
  generation: number;
  prompt: string;
  /** The run that was there before (ended, or being ended): not this one. */
  afterRunId: string | null;
  /** Epoch ms after which Enter is no longer pressed. */
  until: number;
}

/** wait: not yet; enter: press Enter now; gone: that client will not reach
 * its first gate with this instruction (it ended, was frozen, runs another
 * instruction, or is past its first episode); late: GATE_WAIT_MS passed. */
export type GateStep = "wait" | "enter" | "gone" | "late";

export function gateStep(
  wait: GateWait,
  view: RunView | null,
  current: boolean,
  now: number,
): GateStep {
  if (now >= wait.until) return "late";
  if (!view || !current) return "wait";
  const runId = view.run?.runId ?? null;
  if (runId === null || runId === wait.afterRunId) return "wait";
  if (view.run?.form?.prompt !== wait.prompt) return "gone";
  if (view.state === "at_gate") {
    // Only the gate of the new client's first episode: a later one means
    // episodes went by that this page did not see (another tab, a stale read),
    // and the scene was not reset for this sentence. The client counts its
    // episodes per run from 0 (openpi fr3_local eval_session.py).
    const episode = view.episode;
    if (!episode) return "wait";
    if (episode.counted !== 0 || (episode.no ?? 0) > 1) return "gone";
    return view.actions.enter.allowed ? "enter" : "wait";
  }
  if (view.state === "preflight" || view.state === "starting") return "wait";
  return "gone";
}
