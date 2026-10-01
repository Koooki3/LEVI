// A person pressing Run or Resume in the LEVI page while the robot policy
// infers is refused by the live workspace's core (409). The core's sentence
// is for logs; this one says what it means.
const GATE_REFUSAL = /the evaluation is inferring, try again shortly/i;

export function friendlyError(
  message: string,
  t: (text: string) => string,
): string {
  if (GATE_REFUSAL.test(message))
    return t(
      "Paused for the robot: the evaluation is running its policy on the GPU, so this action is refused for a moment. Nothing is lost; try again in a few seconds (between episodes). A run that was already going stops as blocked and continues by itself once the gate has stayed open for a few seconds.",
    );
  return message;
}
