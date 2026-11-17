// The one-click sequences of Step 5 (L2-STEP5-SIMPLIFY-1117): the three
// startup-homing keys o, p, h, and the inventory `y` followed by the release
// Enter of round 1. One click on the page's button is the person's word for the
// whole sequence (the text beside the button says what that word covers); the
// page then sends the keys one at a time through the ordinary key request, so
// everything the backend holds a key to still holds: the seen prompt, the token,
// the delay and the gap, the presence of this tab, one byte per prompt.
//
// What this module decides, from the run document alone, is *when the next key
// may go out*: only when terminal D shows the prompt that key answers, as a
// prompt it printed after the last key of the sequence was sent, and the
// backend offers the key and the page's own rules (`keyVM`) say it is ready.
// Anything else (another state, another prompt, D gone, another run) ends the
// sequence at once and sends nothing more; so does a wait that takes too long.
// Nothing here writes a byte: `useMacro` (use-macro.tsx) calls `pressKey`.
import type { KeyId, RunView, StateId } from "./autonomy-doc";
import {
  keyFitsPrompt,
  requiredAttest,
  type KeyVM,
  type Limits,
} from "./autonomy-logic";

export type MacroId = "home" | "inventory";

export interface MacroStep {
  /** The key of terminal D this step presses. */
  key: KeyId;
  /** The state D must be in, showing that key's prompt, for it to go. */
  state: StateId;
}

/** The sequences, in the order the keys go out. `home`: the three prompts of
 * the startup home (D asks them one after the other and flushes the keys typed
 * before it asks the next, so each goes only on its own prompt). `inventory`:
 * the inventory prompt, then the release prompt of round 1. */
export const MACRO_STEPS: Record<MacroId, readonly MacroStep[]> = {
  home: [
    { key: "home_o", state: "await_home_o" },
    { key: "home_p", state: "await_home_p" },
    { key: "home_h", state: "await_home_h" },
  ],
  inventory: [
    { key: "inventory_yes", state: "await_inventory" },
    { key: "release_round", state: "await_release" },
  ],
};

/** The steps a click may start from: `home` from any of its three prompts (a
 * reload in the middle of it, or a sequence that stopped, continues from where D
 * is), `inventory` only from the inventory prompt: the release prompt has its
 * own one-click button. */
const ENTRY: Record<MacroId, readonly number[]> = {
  home: [0, 1, 2],
  inventory: [0],
};

/** The keys a macro's button stands for (the page shows no separate button for
 * them: the sequence is one decision). */
export const MACRO_OF_KEY: Partial<Record<KeyId, MacroId>> = {
  home_o: "home",
  home_p: "home",
  home_h: "home",
  inventory_yes: "inventory",
};

/** The index of the step a click would start from in this state, or null. */
export function macroEntry(id: MacroId, run: RunView | null): number | null {
  if (!run || !run.d.alive) return null;
  const at = MACRO_STEPS[id].findIndex((step) => step.state === run.state.id);
  return at >= 0 && ENTRY[id].includes(at) ? at : null;
}

/** The macro a run in this state offers, if any. */
export function macroOffered(run: RunView | null): MacroId | null {
  for (const id of Object.keys(MACRO_STEPS) as MacroId[])
    if (macroEntry(id, run) !== null) return id;
  return null;
}

/** A sequence under way: which, in which run, how far, and which prompt the
 * last key it sent answered (the next key must answer a different one). */
export interface MacroRun {
  id: MacroId;
  runId: string;
  /** The index of the step the click started from. */
  from: number;
  /** The index of the step still to send; `steps.length` once all went. */
  next: number;
  /** The id of the prompt the last key sent by this sequence answered. */
  sentPrompt: string | null;
  /** Epoch ms of the start or of the last key sent. */
  since: number;
}

export type MacroEnd =
  | "unexpected_screen"
  | "timeout"
  | "run_changed"
  | "refused"
  | "cancelled";

export type MacroMove =
  | { do: "wait" }
  | { do: "send"; vm: KeyVM; attest: string[] }
  | { do: "done" }
  | { do: "abort"; why: MacroEnd };

export const macroLimitMs = (id: MacroId, limits: Limits): number =>
  id === "home" ? limits.macroHomeMs : limits.macroReleaseMs;

/** What a sequence under way does now, from the document and the state of its
 * keys. A key is sent only when all of these hold: the run is the one the
 * sequence started in and D is alive; D's state is the next step's state and its
 * prompt is that key's prompt; that prompt is not the one the previous key
 * answered (a prompt printed again has a new id); the key's own button state is
 * ready (the backend offers it, the arming delay and the gap have passed, this
 * tab is present, the document is fresh); and no other write is on its way. */
export function macroMove(
  m: MacroRun,
  run: RunView | null,
  vms: readonly KeyVM[],
  now: number,
  working: boolean,
  limits: Limits,
): MacroMove {
  if (!run || run.runId !== m.runId || !run.d.alive)
    return { do: "abort", why: "run_changed" };
  const steps = MACRO_STEPS[m.id];
  if (m.next >= steps.length) return { do: "done" };
  const step = steps[m.next];
  const prompt = run.state.prompt;
  const here =
    run.state.id === step.state &&
    prompt !== null &&
    keyFitsPrompt(run, step.key) &&
    prompt.id !== m.sentPrompt;
  const late = now - m.since > macroLimitMs(m.id, limits);
  if (!here) {
    // D is on its way to the next prompt only if a key of this sequence was
    // sent and D still shows that key's own prompt, or is working (`busy`: the
    // pose check, the vision model). Anything else is not what was expected.
    const going =
      m.sentPrompt !== null &&
      ((prompt !== null && prompt.id === m.sentPrompt) ||
        run.state.id === "busy");
    if (!going) return { do: "abort", why: "unexpected_screen" };
    return late ? { do: "abort", why: "timeout" } : { do: "wait" };
  }
  const vm = vms.find((candidate) => candidate.key.id === step.key);
  if (!vm || vm.mode !== "ready" || working)
    return late ? { do: "abort", why: "timeout" } : { do: "wait" };
  return { do: "send", vm, attest: requiredAttest(step.key) };
}
