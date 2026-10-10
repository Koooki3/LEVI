"use client";
// Step 4 of a single run: make the job file, show the plan, and start after
// the person types the confirmation phrase. On success the page goes to the
// run's own page (/automatic/runs/<id>).
import { useCallback, useEffect, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";
import { Button, Skeleton } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { ApiError, wizardApi, type WizardApi } from "./wizard-api";
import { failureOf, type Failure } from "./wizard-errors";
import { JobAttempts } from "./wizard-job";
import { PhraseConfirm } from "./wizard-confirm";
import {
  IntentKeys,
  buildJobDraft,
  planUsable,
  type WizardForm,
} from "./wizard-logic";
import { PlanCard } from "./wizard-plan";
import type { LaunchPlan, PoliciesResponse } from "./wizard-types";

export function WizardRunConfirm({
  form,
  policies,
  api = wizardApi,
  onLaunched,
  clock = Date.now,
}: {
  form: WizardForm;
  policies: PoliciesResponse | null;
  api?: Pick<WizardApi, "createJob" | "planLaunch" | "launchRun">;
  onLaunched: (runId: string) => void;
  clock?: () => number;
}) {
  const { t } = useLocale();
  const jobs = useRef(new JobAttempts());
  const keys = useRef(new IntentKeys());
  const alive = useRef(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [plan, setPlan] = useState<LaunchPlan | null>(null);
  const [busy, setBusy] = useState<"plan" | "launch" | null>("plan");
  const [failure, setFailure] = useState<{
    what: "plan" | "launch";
    detail: Failure;
  } | null>(null);
  const [now, setNow] = useState(clock);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    if (!plan) return;
    const timer = setInterval(() => setNow(clock()), 1000);
    return () => clearInterval(timer);
  }, [plan, clock]);

  const prepare = useCallback(async () => {
    setBusy("plan");
    setFailure(null);
    setPlan(null);
    try {
      const id = await jobs.current.ensure(api, buildJobDraft(form, policies));
      if (!alive.current) return;
      setJobId(id);
      const next = await api.planLaunch(id, form.executionMode);
      if (!alive.current) return;
      setPlan(next);
      setNow(clock());
    } catch (error) {
      if (alive.current)
        setFailure({ what: "plan", detail: failureOf(error, t) });
    } finally {
      if (alive.current) setBusy(null);
    }
  }, [api, form, policies, clock, t]);

  useEffect(() => {
    void prepare();
    // The plan is made once when the step opens; "Plan again" repeats it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const launch = async () => {
    if (!plan || !jobId || busy) return;
    const intent = `launch:${plan.plan_sha256}:${plan.launch_token}`;
    setBusy("launch");
    setFailure(null);
    try {
      const { run_id } = await api.launchRun(
        jobId,
        plan,
        keys.current.idFor(intent),
      );
      keys.current.release(intent);
      if (alive.current) onLaunched(run_id);
    } catch (error) {
      if (alive.current) {
        const detail = failureOf(error, t);
        setFailure({ what: "launch", detail });
        // A refusal is final for this request; a network failure keeps its
        // request id so that pressing again is the same request.
        if (error instanceof ApiError && error.status !== 0)
          keys.current.release(intent);
      }
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const usable = plan ? planUsable(plan, now) : false;
  const reason = !plan
    ? undefined
    : !plan.launchable
      ? t("automatic.wizard.plan.not_launchable")
      : plan.token_expires_at <= now
        ? t("automatic.wizard.plan.token_expired")
        : undefined;
  const stale =
    failure?.what === "launch" &&
    ["plan_changed", "token_expired"].some((c) =>
      failure.detail.code.toLowerCase().includes(c),
    );

  return (
    <div className="aw-form">
      {busy === "plan" && (
        <div role="status" aria-label={t("automatic.wizard.plan.making")}>
          <Skeleton height={96} />
        </div>
      )}
      {failure && (
        <div>
          <RequestProblem
            action={
              failure.what === "plan"
                ? "automatic.wizard.plan.failed"
                : "automatic.wizard.launch.failed"
            }
            message={failure.detail.message}
          />
          {failure.detail.fields.length > 0 && (
            <ul
              className="aw-list"
              aria-label={t("automatic.wizard.plan.fields")}
            >
              {failure.detail.fields.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
          {stale && (
            <Note tone="warning">{t("automatic.wizard.launch.stale")}</Note>
          )}
        </div>
      )}
      {plan && <PlanCard plan={plan} now={now} />}
      <div className="aw-actions__group">
        <Button
          icon={RotateCcw}
          disabled={busy !== null}
          onClick={() => void prepare()}
        >
          {t("automatic.wizard.plan.again")}
        </Button>
      </div>
      {plan && (
        <PhraseConfirm
          phrase="launch"
          label={t("automatic.wizard.launch.type")}
          hint={t("automatic.wizard.launch.hint")}
          buttonLabel={t("automatic.wizard.launch.button")}
          busy={busy === "launch"}
          disabled={!usable || busy === "plan"}
          disabledReason={reason}
          onConfirm={() => void launch()}
        />
      )}
    </div>
  );
}
