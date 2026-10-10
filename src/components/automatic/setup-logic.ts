// Pure rules of the environment-setup step of the evaluation wizard
// (GET /automatic/setup-guide). The page only ever shows what the guide lists:
// a `copy` step is a command for a person to run in a terminal (LEVI never runs
// it), a `native` step is a status LEVI reads itself, an `execute` step may be
// run by LEVI only when an executor for it exists. Nothing here touches the
// robot: a step that moves it needs a person on site.
import type {
  Bilingual,
  SetupLevel,
  SetupMode,
  SetupStatus,
  SetupStep,
} from "./wizard-types";

/** The text of a bilingual pair in the page language. */
export function pickText(text: Bilingual, language: "en" | "zh"): string {
  return (language === "zh" ? text.zh : text.en) || text.en || text.zh || "";
}

/** The safety items a person ticks before a level-4 command can be copied
 * (the robot moves). Keys of the language catalogue, in this order. */
export const SAFETY_ITEM_KEYS = [
  "automatic.setup.safety.estop",
  "automatic.setup.safety.clear",
  "automatic.setup.safety.one_client",
  "automatic.setup.safety.still",
] as const;

export const SAFETY_ITEM_COUNT = SAFETY_ITEM_KEYS.length;

/** Whether the step's command may only be copied after the safety list. */
export function needsSafetyChecklist(step: SetupStep): boolean {
  return step.level === 4 && step.mode !== "native";
}

/** What a step is offered as. `execute` becomes `copy` when LEVI has no way to
 * run it (no executor wired): the person then runs the command herself. */
export function effectiveMode(step: SetupStep, canExecute: boolean): SetupMode {
  if (step.mode === "execute" && !canExecute) return "copy";
  return step.mode;
}

/** The command can be offered to copy: a copy step with a command, and the
 * safety list ticked when the step moves the robot. */
export function copyAllowed(step: SetupStep, safetyDone: boolean): boolean {
  if (!step.command) return false;
  if (step.mode === "native") return false;
  return needsSafetyChecklist(step) ? safetyDone : true;
}

/** Why the copy button is not available, as a catalogue key (or null). */
export function copyBlockedKey(
  step: SetupStep,
  safetyDone: boolean,
):
  | "automatic.setup.blocked.safety"
  | "automatic.setup.blocked.nocommand"
  | null {
  if (step.mode === "native") return null;
  if (!step.command) return "automatic.setup.blocked.nocommand";
  if (needsSafetyChecklist(step) && !safetyDone)
    return "automatic.setup.blocked.safety";
  return null;
}

const STATUS_ORDER: SetupStatus[] = ["todo", "warn", "unknown", "ok"];

export function countByStatus(steps: SetupStep[]): Record<SetupStatus, number> {
  const out: Record<SetupStatus, number> = {
    ok: 0,
    warn: 0,
    todo: 0,
    unknown: 0,
  };
  for (const step of steps) out[step.status] += 1;
  return out;
}

/** Steps still needing a person: anything not `ok` and not ticked as checked. */
export function openSteps(steps: SetupStep[], checked: ReadonlySet<string>) {
  return steps.filter((step) => step.status !== "ok" && !checked.has(step.id))
    .length;
}

export function firstOpenIndex(
  steps: SetupStep[],
  checked: ReadonlySet<string>,
): number {
  return steps.findIndex(
    (step) => step.status !== "ok" && !checked.has(step.id),
  );
}

export const LEVEL_KEYS: Record<SetupLevel, string> = {
  1: "automatic.setup.level.1",
  2: "automatic.setup.level.2",
  3: "automatic.setup.level.3",
  4: "automatic.setup.level.4",
};

export const STATUS_KEYS: Record<SetupStatus, string> = {
  ok: "automatic.setup.status.ok",
  warn: "automatic.setup.status.warn",
  todo: "automatic.setup.status.todo",
  unknown: "automatic.setup.status.unknown",
};

export { STATUS_ORDER };

/** Steps as the guide gave them, but a step the page cannot make sense of
 * (wrong level or mode) is dropped rather than shown wrongly. */
export function readSteps(raw: unknown): SetupStep[] {
  if (!raw || typeof raw !== "object") return [];
  const list = (raw as { steps?: unknown }).steps;
  if (!Array.isArray(list)) return [];
  const out: SetupStep[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const s = item as Partial<SetupStep>;
    if (
      typeof s.id !== "string" ||
      !s.title ||
      ![1, 2, 3, 4].includes(s.level as number) ||
      !["native", "execute", "copy"].includes(s.mode as string) ||
      !["ok", "warn", "todo", "unknown"].includes(s.status as string)
    )
      continue;
    out.push({
      id: s.id,
      title: s.title,
      level: s.level as SetupLevel,
      mode: s.mode as SetupMode,
      command: typeof s.command === "string" ? s.command : undefined,
      why: s.why ?? { en: "", zh: "" },
      status: s.status as SetupStatus,
      detail: typeof s.detail === "string" ? s.detail : undefined,
    });
  }
  return out;
}
