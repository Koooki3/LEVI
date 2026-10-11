"use client";
import { useEffect, useRef, useState } from "react";
import { Badge, Button } from "@/components/ds";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { SetupCommand } from "./setup-copy";
import { ApiError, errorOf } from "./wizard-api";

export type Fr3Recipe = {
  entrypoint: string;
  check_command: string;
  execute_command_template: string;
  plan_sha256: string;
  hardware_verified: false;
  manual_review_required: true;
  requires_new_plan: boolean;
  blockers: string[];
};
const BLOCKERS: Record<string, string> = {
  reviewed_fr3_binding_required: "A reviewed FR3 binding file is required.",
  operator_terminal_confirmation_required:
    "An operator must confirm in the terminal.",
  physical_validation_not_performed:
    "Physical operation has not been validated.",
  online_C5_provider_required: "A live annotation provider is required.",
  real_scene_provider_required: "A real scene-check provider is required.",
  real_reset_goal_and_event_providers_required:
    "Real reset-goal and event providers are required.",
  reset_policy_deployment_review_required:
    "The reset policy deployment needs review.",
  absolute_rollout_root_required:
    "The frozen plan needs an absolute rollout directory.",
  confirmed_initial_state_contract_required:
    "The frozen plan needs a confirmed Initial State Contract.",
  forward_deployment_identity_required:
    "The frozen plan needs the forward policy deployment identity.",
  reset_deployment_identity_required:
    "The frozen plan needs the reset policy deployment identity.",
  shadow_has_no_motion_authority:
    "Shadow mode does not authorize robot motion.",
};
export function bridgeRecipePath(
  target: { jobId: string } | { campaignId: string; segment?: number },
) {
  return "jobId" in target
    ? `automatic/jobs/${encodeURIComponent(target.jobId)}/fr3-bridge`
    : `automatic/campaigns/${encodeURIComponent(target.campaignId)}/fr3-bridge${target.segment ? "?segment=" + target.segment : ""}`;
}
function valid(value: unknown): value is Fr3Recipe {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    [
      "entrypoint",
      "check_command",
      "execute_command_template",
      "plan_sha256",
    ].every((k) => typeof v[k] === "string") &&
    v.hardware_verified === false &&
    v.manual_review_required === true &&
    typeof v.requires_new_plan === "boolean" &&
    Array.isArray(v.blockers) &&
    v.blockers.every((x) => typeof x === "string")
  );
}
export async function loadBridgeRecipe(
  path: string,
  signal: AbortSignal,
): Promise<Fr3Recipe> {
  const response = await fetch("/api/levi/" + path, {
    method: "GET",
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw await errorOf(response);
  const value: unknown = await response.json();
  if (!valid(value)) throw new Error("Invalid FR3 preparation response");
  return value;
}

/** Reads and copies a recipe only; this component has no execution API. */
export function Fr3BridgeRecipe({
  target,
  load = loadBridgeRecipe,
}: {
  target: { jobId: string } | { campaignId: string; segment?: number };
  load?: typeof loadBridgeRecipe;
}) {
  const { t } = useLocale();
  const path = bridgeRecipePath(target);
  const [answer, setAnswer] = useState<{
    path: string;
    value: Fr3Recipe;
  } | null>(null);
  const [problem, setProblem] = useState<{ path: string; text: string } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort();
    setBusy(false);
    return () => controller.current?.abort();
  }, [path]);
  const read = async () => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setBusy(true);
    setProblem(null);
    setAnswer(null);
    try {
      const value = await load(path, next.signal);
      if (!next.signal.aborted) setAnswer({ path, value });
    } catch (e) {
      if (!next.signal.aborted) {
        const text =
          e instanceof ApiError && e.status === 404
            ? t(
                "This backend or retained job does not provide FR3 preparation.",
              )
            : e instanceof Error
              ? e.message
              : String(e);
        setProblem({ path, text });
      }
    } finally {
      if (!next.signal.aborted) setBusy(false);
    }
  };
  const recipe = answer?.path === path ? answer.value : null;
  return (
    <section className="pg-fr3-recipe">
      <h2>{t("FR3 execution preparation")}</h2>
      <p>
        {t(
          "Real AERI software bridge; physical operation has not been accepted. Review the binding and confirm in your terminal.",
        )}
      </p>
      <Button size="sm" loading={busy} onClick={() => void read()}>
        {t(recipe ? "Refresh preparation" : "Load preparation")}
      </Button>
      {problem?.path === path && (
        <RequestProblem
          action="FR3 execution preparation"
          message={problem.text}
        />
      )}
      {recipe && (
        <>
          <p>
            <Badge tone="warning">{t("Hardware unverified")}</Badge>{" "}
            <Badge>{t("Manual review required")}</Badge>
          </p>
          {recipe.requires_new_plan && (
            <Note tone="warning">
              {t(
                "Create a new plan with the missing fields and a confirmed Initial State Contract. Keep the frozen job unchanged.",
              )}
            </Note>
          )}
          <ul>
            {recipe.blockers.map((code) => (
              <li key={code}>
                {t(BLOCKERS[code] || "Preparation requirement")}:{" "}
                <code>{code}</code>
              </li>
            ))}
          </ul>
          <p>
            {t("Plan digest")}: <code>{recipe.plan_sha256}</code>
          </p>
          <h3>{t("Check in terminal")}</h3>
          <SetupCommand
            command={recipe.check_command}
            label={t("Check in terminal")}
          />
          <details>
            <summary>{t("Execution template for manual review")}</summary>
            <p>
              {t(
                "Replace the reviewed binding placeholder and use the binding digest returned by the check. The template still requires human terminal confirmation.",
              )}
            </p>
            <SetupCommand
              command={recipe.execute_command_template}
              label={t("Execution template for manual review")}
            />
          </details>
        </>
      )}
    </section>
  );
}
