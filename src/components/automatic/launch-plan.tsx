"use client";
// The plan card: what a launch would do, the checks and refusals, and the
// launch button. Pure view; the panel owns the requests.
import { CircleAlert, CircleCheck, Info, TriangleAlert } from "lucide-react";
import { Button, Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { movesRobot } from "./run-logic";
import { ModeChips, Note } from "./run-parts";
import type { LaunchPlan } from "./types";

const SEVERITY_ICON = {
  info: Info,
  warn: TriangleAlert,
  error: CircleAlert,
} as const;

/** Why the launch button is not usable now, as a catalogue key; null when it is. */
export function launchBlock(
  plan: LaunchPlan,
  now: number,
): "real" | "refused" | "expired" | null {
  if (movesRobot(plan.execution_mode) || plan.motion) return "real";
  if (!plan.launchable) return "refused";
  if (plan.token_expires_at && plan.token_expires_at <= now) return "expired";
  return null;
}
const SEVERITY_KEY = {
  info: "automatic.run.check.info",
  warn: "automatic.run.check.warn",
  error: "automatic.run.check.error",
} as const;
const BLOCK_KEY = {
  real: "automatic.run.launch.block.real",
  refused: "automatic.run.launch.block.refused",
  expired: "automatic.run.launch.block.expired",
} as const;

export function PlanCard({
  plan,
  now,
  launching,
  onLaunch,
}: {
  plan: LaunchPlan;
  now: number;
  launching: boolean;
  onLaunch: () => void;
}) {
  const { t } = useLocale();
  const block = launchBlock(plan, now);
  const yes = t("automatic.run.yes");
  const no = t("automatic.run.no");
  return (
    <section className="ar-section" aria-labelledby="ar-plan-title">
      <h3 id="ar-plan-title">{t("automatic.run.plan.title")}</h3>
      <ModeChips execution={plan.execution_mode} reset={plan.reset_mode} />
      <dl className="ar-dl">
        <dt>{t("automatic.run.plan.episodes")}</dt>
        <dd>{plan.episodes}</dd>
        <dt>{t("automatic.run.plan.roles")}</dt>
        <dd>
          {plan.roles
            .map((role) =>
              role === "forward"
                ? t("automatic.run.plan.role.forward")
                : role === "reset"
                  ? t("automatic.run.plan.role.reset")
                  : role,
            )
            .join(", ")}
        </dd>
        {plan.scene_check && (
          <>
            <dt>{t("automatic.run.plan.scene_check")}</dt>
            <dd>
              {plan.scene_check === "operator_attested"
                ? t("automatic.run.plan.scene_check.operator")
                : t("automatic.run.plan.scene_check.provider")}
            </dd>
          </>
        )}
        <dt>{t("automatic.run.plan.isc")}</dt>
        <dd>
          {plan.isc
            ? `${plan.isc.id}@${plan.isc.version} · ${
                plan.isc.status === "draft"
                  ? t("automatic.run.plan.isc.draft")
                  : t("automatic.run.plan.isc.confirmed")
              }`
            : t("automatic.run.plan.isc.none")}
        </dd>
        <dt>{t("automatic.run.plan.motion")}</dt>
        <dd>{plan.motion ? yes : no}</dd>
        {plan.uses_live && (
          <>
            <dt>{t("automatic.run.plan.uses_live")}</dt>
            <dd>
              {[
                plan.uses_live.c5 ? t("automatic.run.plan.live.c5") : "",
                plan.uses_live.gate ? t("automatic.run.plan.live.gate") : "",
              ]
                .filter(Boolean)
                .join(", ") || no}
            </dd>
          </>
        )}
      </dl>
      {plan.isc?.status === "draft" && (
        <Note
          tone="warning"
          icon={TriangleAlert}
          title={t("automatic.run.isc.draft")}
        >
          {t("automatic.run.isc.draft_detail")}
        </Note>
      )}
      {plan.checks.length > 0 && (
        <ul className="ar-checks" aria-label={t("automatic.run.plan.checks")}>
          {plan.checks.map((check) => (
            <li key={check.code}>
              <Icon
                icon={check.ok ? CircleCheck : SEVERITY_ICON[check.severity]}
                label={
                  check.ok
                    ? t("automatic.run.check.ok")
                    : t(SEVERITY_KEY[check.severity])
                }
              />
              <span>
                <code>{check.code}</code>
                {check.detail ? `: ${check.detail}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
      {plan.refusals.length > 0 && (
        <Note
          tone="danger"
          role="alert"
          icon={CircleAlert}
          title={t("automatic.run.plan.refusals")}
        >
          <ul className="ar-checks">
            {plan.refusals.map((code) => (
              <li key={code}>
                <code>{code}</code>
              </li>
            ))}
          </ul>
        </Note>
      )}
      <div className="ar-actions">
        <Button
          variant="primary"
          loading={launching}
          disabled={block !== null}
          aria-describedby={block ? "ar-launch-why" : undefined}
          onClick={onLaunch}
        >
          {t("automatic.run.launch.button")}
        </Button>
        {block && (
          <span className="ar-muted" id="ar-launch-why">
            {t(BLOCK_KEY[block])}
          </span>
        )}
      </div>
    </section>
  );
}
