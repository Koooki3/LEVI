// The refusals of the arm stack's and the hosted run's writes in words. The
// backend names each refusal with a stable code (`stack.py` REFUSALS,
// `stack_act.py`, `run.py`) and the facts as `extra`; the page words the codes
// it knows from the catalogue (`<prefix><code>`, the facts as placeholders)
// and falls back to the generic refusals of the other action cards
// (marvel0-failure.ts: no answer, a body not accepted, the guard of the
// person, ...). A code it knows nowhere shows the server's English sentence,
// marked as such by the caller. Pure.
import { codedSay, paramsOf, type Known } from "./ctl-text";
import type { ApiFailure } from "./deploy-api";
import { sayFailure } from "./marvel0-failure";
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";

/** The sentences of the stack's refusals: the controls' own (the same family
 * as the blockers, `marvel0.ctl.block.`), then the monitor's and the marker's
 * of the S4-0 card (`marvel0.stack.refuse.`). */
export const STACK_REFUSE = ["marvel0.ctl.block.", "marvel0.stack.refuse."];
/** The sentences of the run's refusals: its own, then the reasons an action is
 * not offered (a request for an action that is not offered is refused with the
 * same code). */
export const RUN_REFUSE = ["marvel0.run.refuse.", "marvel0.run.why."];

export function sayRefusal(
  failure: ApiFailure,
  prefixes: readonly string[],
  known: Known,
): Worded<Say> {
  // A write with no answer may still have been carried out.
  if (failure.code === "network") return sayFailure(failure, { write: true });
  const said = codedSay(
    prefixes,
    failure.code,
    failure.message,
    paramsOf(failure.extra),
    known,
  );
  // Not a code of the controls (or its facts did not come): the generic
  // refusals of the other action cards, else the server's own sentence.
  return said.say ? said : sayFailure(failure, { write: true });
}
