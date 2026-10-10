"use client";
// The plan card of a launch or a campaign: what will run, the checks the
// server made, and why it refuses (if it does). It only shows the server's
// answer; it decides nothing.
import { CircleCheck, CircleX, Info, TriangleAlert } from "lucide-react";
import { Badge, Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { tokenSecondsLeft } from "./wizard-logic";
import type { LaunchPlan, PlanCheck } from "./wizard-types";

const REFUSAL_KEYS: Record<string, string> = {
  no_robot_adapter: "automatic.wizard.refusal.no_robot_adapter",
  robot_busy: "automatic.wizard.refusal.robot_busy",
  real_mode: "automatic.wizard.refusal.real_mode",
};

/** A refusal code in words when the page knows it; the code itself shows
 * beside the words, so nothing the server said is lost. */
export function Refusals({ codes }: { codes: string[] }) {
  const { t } = useLocale();
  if (!codes.length) return null;
  return (
    <Note tone="warning" role="alert">
      <strong>{t("automatic.wizard.plan.refused")}</strong>
      <ul className="aw-list">
        {codes.map((code) => (
          <li key={code}>
            {REFUSAL_KEYS[code] ? `${t(REFUSAL_KEYS[code])} ` : ""}
            <code>{code}</code>
          </li>
        ))}
      </ul>
    </Note>
  );
}

export function ChecksList({ checks }: { checks: PlanCheck[] }) {
  const { t } = useLocale();
  if (!checks.length) return null;
  return (
    <ul className="aw-checks" aria-label={t("automatic.wizard.plan.checks")}>
      {checks.map((check) => {
        const icon = check.ok
          ? CircleCheck
          : check.severity === "error"
            ? CircleX
            : check.severity === "warn"
              ? TriangleAlert
              : Info;
        return (
          <li
            key={check.code}
            data-ok={check.ok}
            data-severity={check.severity}
          >
            <Icon icon={icon} />
            <span>
              <code>{check.code}</code>
              {check.detail ? ` · ${check.detail}` : ""}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

export function PlanCard({ plan, now }: { plan: LaunchPlan; now: number }) {
  const { t } = useLocale();
  const left = tokenSecondsLeft(plan.token_expires_at, now);
  return (
    <section className="aw-plan" aria-label={t("automatic.wizard.plan.title")}>
      <dl className="pg-summary">
        <div>
          <dt>{t("automatic.wizard.plan.run")}</dt>
          <dd>
            <code>{plan.run_id}</code>
          </dd>
        </div>
        <div>
          <dt>{t("automatic.wizard.plan.mode")}</dt>
          <dd>
            <code>{plan.execution_mode}</code>
          </dd>
        </div>
        <div>
          <dt>{t("automatic.wizard.plan.reset")}</dt>
          <dd>
            <code>{plan.reset_mode}</code> · <code>{plan.scene_check}</code>
          </dd>
        </div>
        <div>
          <dt>{t("automatic.wizard.plan.policies")}</dt>
          <dd>{plan.roles.join(" + ")}</dd>
        </div>
        <div>
          <dt>{t("automatic.wizard.plan.episodes")}</dt>
          <dd>{plan.episodes}</dd>
        </div>
        <div>
          <dt>{t("automatic.wizard.plan.digest")}</dt>
          <dd>
            <code>{plan.plan_sha256.slice(0, 16)}</code>
          </dd>
        </div>
      </dl>
      <p className="aw-plan__badges">
        <Badge tone={plan.motion ? "warning" : "success"}>
          {plan.motion
            ? t("automatic.wizard.plan.motion")
            : t("automatic.wizard.plan.no_motion")}
        </Badge>
        {plan.isc && (
          <Badge tone={plan.isc.status === "confirmed" ? "success" : "warning"}>
            {plan.isc.status === "confirmed"
              ? t("automatic.wizard.plan.isc_confirmed")
              : t("automatic.wizard.plan.isc_draft")}
          </Badge>
        )}
        {plan.uses_live.c5 && (
          <Badge tone="info">{t("automatic.wizard.plan.uses_judge")}</Badge>
        )}
        {plan.uses_live.gate && (
          <Badge tone="info">{t("automatic.wizard.plan.uses_gate")}</Badge>
        )}
      </p>
      <ChecksList checks={plan.checks} />
      <Refusals codes={plan.refusals} />
      <p className="pg-pool-hint" role="status">
        {left > 0
          ? t("automatic.wizard.plan.token_left").replace("{s}", String(left))
          : t("automatic.wizard.plan.token_expired")}
      </p>
    </section>
  );
}
