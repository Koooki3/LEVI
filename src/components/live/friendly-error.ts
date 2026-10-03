// A person pressing Run or Resume in the LEVI page while the robot policy
// infers is refused by the live workspace's core (409). The core's sentence
// is for logs; this one says what it means.
const GATE_REFUSAL = /the evaluation is inferring, try again shortly/i;
// Removing an episode that belongs to the batch in progress (409).
const BATCH_REFUSAL = /being labelled \(part of the batch in progress\)/i;
const NEVER_TAKEN = /was never taken into the dataset/i;

export function friendlyError(
  message: string,
  t: (text: string) => string,
): string {
  if (GATE_REFUSAL.test(message))
    return t(
      "Paused for the robot: the evaluation is running its policy on the GPU, so this action is refused for a moment. Nothing is lost; try again in a few seconds (between episodes). A run that was already going stops as blocked and continues by itself once the gate has stayed open for a few seconds.",
    );
  if (BATCH_REFUSAL.test(message))
    return t(
      "Being labelled right now (part of the batch in progress). Try again when the batch has finished; nothing was removed.",
    );
  if (NEVER_TAKEN.test(message))
    return t(
      "This episode was never taken into the dataset, so there is nothing to remove.",
    );
  return message;
}
