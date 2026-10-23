import { isQuiet, type RunView } from "./run-logic";

/** Only a completed, non-moving run with the same identity and a released
 * supervisor may seed the next command. */
export function confirmedRunExit(view: RunView, runId: string): boolean {
  return (
    view.run?.runId === runId &&
    view.run.phase === "ended" &&
    view.run.lockReleased === true &&
    view.state === "done" &&
    isQuiet(view.state) &&
    !view.mayMove
  );
}

/** Preserve the saved request body exactly, changing only its instruction. */
export function bodyWithNextPrompt(
  form: Record<string, unknown>,
  prompt: string,
): Record<string, unknown> {
  return { ...form, prompt };
}
