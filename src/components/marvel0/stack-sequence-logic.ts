// What the progress of a guided start or stop shows, from the sequence the
// backend reports. Pure. The backend marks the steps after the one a sequence
// ended at `not_reached`; a step it still calls pending once the sequence is
// over is read the same way (never as "waiting"), so a list never promises a
// step that will not come. The words of a finished stop are made from the steps
// that were done or skipped, never from the plan.
import {
  type CompId,
  type SequenceAct,
  type SequenceStep,
  type SequenceStepState,
  type Tone,
} from "./stack-act-logic";

/** The sequence is over: nothing it has not done will be done. */
export const sequenceEnded = (seq: SequenceAct): boolean =>
  seq.state === "done" || seq.state === "failed" || seq.state === "cancelled";

/** The state of a step as the card shows it. */
export function shownState(
  seq: SequenceAct,
  item: SequenceStep,
): SequenceStepState {
  return item.state === "pending" && sequenceEnded(seq)
    ? "not_reached"
    : item.state;
}

export const STEP_TONE: Record<SequenceStepState, Tone> = {
  pending: "neutral",
  running: "info",
  done: "success",
  skipped: "neutral",
  failed: "danger",
  cancelled: "warning",
  not_reached: "neutral",
};

/** The step the sequence failed at, when it did. */
export function failedStep(seq: SequenceAct): SequenceStep | null {
  return seq.state === "failed"
    ? (seq.steps.find((item) => item.state === "failed") ?? null)
    : null;
}

/** How a finished stop went, terminal by terminal: those stopped now and
 * those that had nothing of this page's to stop. */
export interface StopSummary {
  stopped: CompId[];
  already: CompId[];
}
export function stopSummary(seq: SequenceAct): StopSummary {
  const of = (state: SequenceStepState): CompId[] =>
    seq.steps.flatMap((item) => (item.state === state ? [item.terminal] : []));
  return { stopped: of("done"), already: of("skipped") };
}
