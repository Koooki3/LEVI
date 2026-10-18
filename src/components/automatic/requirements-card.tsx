"use client";
// What this evaluation session was asked to do: the task texts, the number of
// episodes, the step limits, early stopping, the reset mode, the policies,
// a subtask loop's settings and the inference mode. It shows the server's `requirements` (read from the
// frozen plan) and decides nothing. An older server sends none of it, or only
// part of it: a missing value is left out, never guessed.
import { useLocale } from "@/components/levi-locale";
import type { Requirements } from "./types";
import { planValue } from "./wizard-plan";

type T = (key: string) => string;

const INFERENCE_KEYS: Record<string, string> = {
  legacy_async: "automatic.req.inference.legacy_async",
  event_pause_judge: "automatic.req.inference.event_pause_judge",
};

function policyText(
  policy: { checkpoint_id?: string | null; config?: string | null } | null,
  t: T,
) {
  if (!policy?.checkpoint_id && !policy?.config)
    return t("automatic.req.policy_unrecorded");
  return [policy.checkpoint_id, policy.config].filter(Boolean).join(" · ");
}

/** True when the server sent anything worth a card. */
export function hasRequirements(need?: Requirements | null): boolean {
  return Boolean(need && Object.keys(need).length > 0);
}

export function RequirementsCard({
  requirements,
  className = "",
}: {
  requirements?: Requirements | null;
  className?: string;
}) {
  const { t } = useLocale();
  if (!hasRequirements(requirements)) return null;
  const need = requirements as Requirements;
  const task = need.task ?? {};
  const steps = need.max_steps ?? {};
  // No reset policy runs in a manual reset or a subtask loop (whose reverse
  // loop puts the scene back with subtasks).
  const subtaskLoop = need.reset_mode === "subtask_loop";
  const manual = need.reset_mode === "human_assisted" || subtaskLoop;
  const rows: { key: string; label: string; value: string }[] = [];
  const add = (key: string, label: string, value: string | number | null) => {
    if (value !== null && value !== undefined && value !== "")
      rows.push({ key, label: t(label), value: String(value) });
  };
  add("instruction", "automatic.req.instruction", task.instruction ?? null);
  if (!manual)
    add(
      "reset_instruction",
      "automatic.req.reset_instruction",
      task.reset_instruction ?? null,
    );
  add("episodes", "automatic.req.episodes", need.episodes ?? null);
  add(
    "forward_steps",
    "automatic.req.max_steps_forward",
    steps.forward ?? null,
  );
  if (!manual)
    add("reset_steps", "automatic.req.max_steps_reset", steps.reset ?? null);
  if (typeof need.allow_early_stop === "boolean")
    add(
      "early_stop",
      "automatic.req.early_stop",
      t(
        need.allow_early_stop
          ? "automatic.req.early_stop.allowed"
          : "automatic.req.early_stop.forbidden",
      ),
    );
  if (need.reset_mode)
    add(
      "reset_mode",
      "automatic.req.reset_mode",
      planValue("reset", need.reset_mode, t),
    );
  if (need.policies)
    add(
      "forward_policy",
      "automatic.req.policy_forward",
      policyText(need.policies.forward ?? null, t),
    );
  if (need.policies && !manual)
    add(
      "reset_policy",
      "automatic.req.policy_reset",
      policyText(need.policies.reset ?? null, t),
    );
  const loop = subtaskLoop ? need.subtask_loop : null;
  if (loop) {
    const gib = (value: number) =>
      t("automatic.req.subtask.gib").replace("{gib}", String(value));
    add("subtask_task", "automatic.req.subtask.task", loop.task ?? null);
    add("subtask_rounds", "automatic.req.subtask.rounds", loop.rounds ?? null);
    add(
      "subtask_max_steps",
      "automatic.req.subtask.max_steps",
      loop.subtask_max_steps ?? null,
    );
    add(
      "subtask_max_retries",
      "automatic.req.subtask.max_retries",
      loop.subtask_max_retries ?? null,
    );
    if ("max_subtasks_per_loop" in loop)
      add(
        "subtask_max_per_loop",
        "automatic.req.subtask.max_per_loop",
        loop.max_subtasks_per_loop ?? t("automatic.req.subtask.off"),
      );
    if (typeof loop.home_after_loop === "boolean")
      add(
        "subtask_home",
        "automatic.req.subtask.home_after_loop",
        t(
          loop.home_after_loop
            ? "automatic.req.subtask.yes"
            : "automatic.req.subtask.no",
        ),
      );
    add(
      "subtask_vlm_mem",
      "automatic.req.subtask.vlm_mem",
      typeof loop.vlm_min_free_mem_gib === "number"
        ? gib(loop.vlm_min_free_mem_gib)
        : null,
    );
  }
  if (need.inference?.mode) {
    const mode = INFERENCE_KEYS[need.inference.mode]
      ? t(INFERENCE_KEYS[need.inference.mode])
      : need.inference.mode;
    add(
      "inference",
      "automatic.req.inference_mode",
      need.inference.physical_hold
        ? `${mode} · ${t("automatic.req.physical_hold")}`
        : mode,
    );
  }
  return (
    <section
      className={className}
      aria-label={t("automatic.req.title")}
      data-testid="requirements"
    >
      <h3 className="pg-pool-hint">{t("automatic.req.title")}</h3>
      <dl className="pg-summary">
        {rows.map((row) => (
          <div key={row.key} data-row={row.key}>
            <dt>{row.label}</dt>
            <dd>{row.value}</dd>
          </div>
        ))}
        {task.success_condition && (
          <div data-row="success_condition">
            <dt>{t("automatic.req.success_condition")}</dt>
            <dd>
              {task.success_condition}
              <span className="pg-pool-hint" role="note">
                {" "}
                {t("automatic.req.success_condition_note")}
              </span>
            </dd>
          </div>
        )}
      </dl>
    </section>
  );
}
