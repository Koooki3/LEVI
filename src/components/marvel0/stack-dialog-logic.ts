// What each confirm window of the arm stack says and asks: the fixed
// sentences (a hand on the physical e-stop; what the gates are worth), the
// hazard line of the action, the checks only a person can make (tick boxes,
// recorded with the request) and the confirmation phrase the page sends
// itself. Nothing is typed, except the override of a stop of C with a client
// connected. Pure: the window component prints this and sends nothing the
// spec does not name.
//
// The hazard lines are the page's own words about what the action does to the
// machine; none of them has been checked on the real machine yet (starting
// and stopping from the page is new), and the dialog says so.
import type { Say } from "./marvel0-logic";
import {
  compPhrase,
  phraseFor,
  stopControl,
  type ActView,
  type CompId,
} from "./stack-act-logic";
import type { GripperAction, MarkerOp } from "./stack-api";

export type DialogKind =
  | { kind: "start"; comp: CompId }
  | { kind: "stop"; comp: CompId }
  | { kind: "marker"; op: MarkerOp }
  | { kind: "gripper"; action: GripperAction }
  | { kind: "sequence" }
  | { kind: "sequence_stop" };

export interface Attest {
  /** The key sent in `attest`. */
  code: string;
  label: Say;
}

export interface DialogSpec {
  title: Say;
  intro: Say | null;
  /** Lines about what the action does to the machine, most important first. */
  hazards: Say[];
  /** A hand on the physical e-stop, and the gates' honest limits. */
  estop: boolean;
  attest: Attest[];
  /** The confirmation phrase of the backend's catalogue. The page sends it
   * when the confirm button is clicked; nobody types it. */
  phrase: string;
  confirmKey: string;
  danger: boolean;
  /** The phrase that overrides the client rule of a stop of C, and whether
   * the stop is blocked by that rule now (then it is asked for at once). This
   * is the one phrase a person types: it cuts a client off. */
  override: { phrase: string; now: boolean } | null;
  /** The window offers the choice to pause before B and before C (the whole
   * stack's start; off by default). It is a choice, not a check: Confirm does
   * not wait for it. */
  pauseOption: boolean;
}

const k = (key: string, params?: Record<string, string | number | Say>): Say =>
  params ? { key, params } : { key };

const NAME = (id: CompId): Say => k(`marvel0.ctl.name.${id}`);

/** The hazard lines of starting or stopping a terminal. */
const START_HAZARD: Record<CompId, string[]> = {
  M: [],
  A: ["marvel0.ctl.hazard.start_A"],
  B: ["marvel0.ctl.hazard.start_B"],
  C: ["marvel0.ctl.hazard.start_C_sag", "marvel0.ctl.hazard.start_C_port"],
};
const STOP_HAZARD: Record<CompId, string[]> = {
  M: ["marvel0.ctl.hazard.stop_M"],
  A: ["marvel0.ctl.hazard.stop_A"],
  B: ["marvel0.ctl.hazard.stop_B"],
  C: ["marvel0.ctl.hazard.stop_C"],
};

const attestOf = (keys: string[]): Attest[] =>
  keys.map((code) => ({ code, label: k(`marvel0.ctl.attest.${code}`) }));

/** The hazard lines of the terminals the whole stack's start brings up, A
 * then B then C: each as its own single start's window says it. */
const SEQUENCE_HAZARD: string[] = ["A", "B", "C"].flatMap(
  (id) => START_HAZARD[id as CompId],
);
/** The checks of the whole stack's start: those of A, B and C, each once, in
 * the order the terminals come up (the backend names them per terminal). */
export function sequenceAttest(act: ActView | null): string[] {
  const keys = (["A", "B", "C"] as const).flatMap(
    (id) => act?.comps[id].attest ?? [],
  );
  return [...new Set(keys)];
}

export function dialogSpec(want: DialogKind, act: ActView | null): DialogSpec {
  switch (want.kind) {
    case "start":
      return {
        title: k("marvel0.ctl.dialog.start_title", { name: NAME(want.comp) }),
        intro: k("marvel0.ctl.dialog.start_intro"),
        hazards: START_HAZARD[want.comp].map((key) => k(key)),
        estop: want.comp !== "M",
        attest: attestOf(act?.comps[want.comp].attest ?? []),
        phrase: compPhrase(act, "start", want.comp),
        confirmKey: "marvel0.ctl.btn.start",
        danger: false,
        override: null,
        pauseOption: false,
      };
    case "stop": {
      const comp = act?.comps[want.comp];
      const overridden = comp?.override ?? null;
      return {
        title: k("marvel0.ctl.dialog.stop_title", { name: NAME(want.comp) }),
        intro: k("marvel0.ctl.dialog.stop_intro"),
        hazards: STOP_HAZARD[want.comp].map((key) => k(key)),
        estop: want.comp !== "M",
        attest: [],
        phrase: compPhrase(act, "stop", want.comp),
        confirmKey: "marvel0.ctl.btn.stop",
        danger: true,
        override: overridden
          ? {
              phrase: overridden,
              now: comp ? stopControl(comp).needsOverride : false,
            }
          : null,
        pauseOption: false,
      };
    }
    case "gripper":
      return {
        title: k(
          want.action === "open"
            ? "marvel0.ctl.dialog.gripper_open_title"
            : "marvel0.ctl.dialog.gripper_close_title",
        ),
        intro: k("marvel0.ctl.dialog.gripper_intro"),
        hazards: [k("marvel0.ctl.hazard.gripper")],
        estop: true,
        attest: attestOf(act?.gripper.attest ?? []),
        phrase: phraseFor(act, `gripper_${want.action}`),
        confirmKey:
          want.action === "open"
            ? "marvel0.ctl.btn.gripper_open"
            : "marvel0.ctl.btn.gripper_close",
        danger: false,
        override: null,
        pauseOption: false,
      };
    case "marker": {
      const name = {
        touch: "marker_touch",
        adopt: "marker_adopt",
        release: "marker_clear",
      }[want.op];
      return {
        title: k(`marvel0.ctl.dialog.marker_${want.op}_title`),
        intro: k(`marvel0.ctl.dialog.marker_${want.op}_intro`),
        hazards: [],
        estop: false,
        attest: [],
        phrase: phraseFor(act, name),
        confirmKey: `marvel0.ctl.btn.marker_${want.op}`,
        danger: want.op === "release",
        override: null,
        pauseOption: false,
      };
    }
    case "sequence":
      // One window for the whole chain: the hazard sentences and the checks
      // of A, B and C, all of them, because nothing asks again between the
      // steps (unless the person asks for the pause).
      return {
        title: k("marvel0.ctl.dialog.sequence_title"),
        intro: k("marvel0.ctl.dialog.sequence_intro"),
        hazards: [
          k("marvel0.ctl.hazard.sequence_through"),
          ...SEQUENCE_HAZARD.map((key) => k(key)),
        ],
        estop: true,
        attest: attestOf(sequenceAttest(act)),
        phrase: phraseFor(act, "sequence_start"),
        confirmKey: "marvel0.ctl.btn.sequence",
        danger: false,
        override: null,
        pauseOption: true,
      };
    case "sequence_stop":
      return {
        title: k("marvel0.ctl.dialog.stop_all_title"),
        intro: k("marvel0.ctl.dialog.stop_all_intro"),
        hazards: [
          k("marvel0.ctl.hazard.stop_all_order"),
          k("marvel0.ctl.hazard.stop_all_arm"),
          k("marvel0.ctl.hazard.stop_all_client"),
        ],
        estop: true,
        attest: [],
        phrase: phraseFor(act, "sequence_stop"),
        confirmKey: "marvel0.ctl.btn.stop_all",
        danger: true,
        override: null,
        pauseOption: false,
      };
  }
}

/** Whether the override phrase is in order: typed exactly (spaces at the ends
 * do not count) when the stop is blocked by the client rule, else not asked. */
export function overrideReady(
  spec: DialogSpec,
  typedOverride: string,
  overrideAsked: boolean,
): boolean {
  if (!spec.override?.now && !overrideAsked) return true;
  return typedOverride.trim() === spec.override?.phrase;
}

/** Whether a window's confirm button is on: every check ticked and the
 * override in order. There is no other phrase to type. */
export function dialogReady(
  spec: DialogSpec,
  ticked: Record<string, boolean>,
  typedOverride: string,
  overrideAsked: boolean,
): boolean {
  if (!spec.attest.every((a) => ticked[a.code] === true)) return false;
  return overrideReady(spec, typedOverride, overrideAsked);
}

/** The `attest` object of a request: every check of the spec, true. */
export const attestBody = (spec: DialogSpec): Record<string, boolean> =>
  Object.fromEntries(spec.attest.map((a) => [a.code, true]));
