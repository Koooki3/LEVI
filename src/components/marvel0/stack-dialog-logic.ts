// What each confirmation dialog of the arm stack says and asks: the fixed
// sentences (a hand on the physical e-stop; what the gates are worth), the
// hazard line of the action, the checks only a person can make (tick boxes,
// recorded with the request) and the phrase to type. Pure: the dialog
// component prints this and sends nothing the spec does not name.
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
  | { kind: "sequence" };

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
  phrase: string;
  /** The person types the phrase. When not, the confirm click is the
   * confirmation and the page sends the phrase itself (the gripper test and
   * adopting or setting a marker: a tick or a click, not a sentence). */
  typed: boolean;
  confirmKey: string;
  danger: boolean;
  /** The phrase that overrides the client rule of a stop of C, and whether
   * the stop is blocked by that rule now (then it is asked for at once). */
  override: { phrase: string; now: boolean } | null;
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
        typed: true,
        confirmKey: "marvel0.ctl.btn.start",
        danger: false,
        override: null,
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
        typed: true,
        confirmKey: "marvel0.ctl.btn.stop",
        danger: true,
        override: overridden
          ? {
              phrase: overridden,
              now: comp ? stopControl(comp).needsOverride : false,
            }
          : null,
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
        typed: false,
        confirmKey:
          want.action === "open"
            ? "marvel0.ctl.btn.gripper_open"
            : "marvel0.ctl.btn.gripper_close",
        danger: false,
        override: null,
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
        typed: want.op === "release",
        confirmKey: `marvel0.ctl.btn.marker_${want.op}`,
        danger: want.op === "release",
        override: null,
      };
    }
    case "sequence":
      return {
        title: k("marvel0.ctl.dialog.sequence_title"),
        intro: k("marvel0.ctl.dialog.sequence_intro"),
        hazards: [k("marvel0.ctl.hazard.sequence")],
        estop: true,
        attest: attestOf(act?.comps.A.attest ?? []),
        phrase: phraseFor(act, "sequence_start"),
        typed: true,
        confirmKey: "marvel0.ctl.btn.sequence",
        danger: false,
        override: null,
      };
  }
}

/** Whether a dialog's confirm button is on: the phrase typed exactly (spaces
 * at the ends do not count), every check ticked, and the override phrase when
 * the stop is blocked by the client rule. */
export function dialogReady(
  spec: DialogSpec,
  typed: string,
  ticked: Record<string, boolean>,
  typedOverride: string,
  overrideAsked: boolean,
): boolean {
  if (spec.typed && typed.trim() !== spec.phrase) return false;
  if (!spec.attest.every((a) => ticked[a.code] === true)) return false;
  if (
    (spec.override?.now || overrideAsked) &&
    typedOverride.trim() !== spec.override?.phrase
  )
    return false;
  return true;
}

/** The `attest` object of a request: every check of the spec, true. */
export const attestBody = (spec: DialogSpec): Record<string, boolean> =>
  Object.fromEntries(spec.attest.map((a) => [a.code, true]));
