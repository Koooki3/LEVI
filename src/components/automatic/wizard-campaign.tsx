"use client";
// Step 4 of a multi-model plan. The shared settings were made into a job file;
// here the person sets trials, order, the main measure and alpha, and sees the
// plan from the server change as she edits (schedule, number of policy
// switches, the power table). Starting asks for the phrase `start-campaign`.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge,
  Field,
  Input,
  Radio,
  RadioGroup,
  Select,
  Skeleton,
  Switch,
  Table,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { failureText } from "./wizard-errors";
import { ApiError, wizardApi, type WizardApi } from "./wizard-api";
import { PhraseConfirm } from "./wizard-confirm";
import { JobAttempts } from "./wizard-job";
import {
  IntentKeys,
  LABEL_BASES,
  SCHEDULE_KINDS,
  buildCampaignRequest,
  buildJobDraft,
  hasProblems,
  powerTable,
  validateCampaign,
  type WizardForm,
} from "./wizard-logic";
import { ChecksList, Refusals } from "./wizard-plan";
import type { CampaignPlan, PoliciesResponse } from "./wizard-types";

const DEBOUNCE_MS = 400;

type SetForm = (change: Partial<WizardForm>) => void;

export function PowerTable({ power }: { power: CampaignPlan["power"] }) {
  const { t } = useLocale();
  const { columns, cells } = useMemo(
    () => powerTable(power.rows),
    [power.rows],
  );
  return (
    <div className="aw-power">
      <p>
        {power.detectable_difference == null
          ? t("automatic.wizard.power.unknown")
          : t("automatic.wizard.power.detectable").replace(
              "{d}",
              String(Math.round(power.detectable_difference * 1000) / 1000),
            )}
      </p>
      {columns.length > 0 && (
        <Table caption={t("automatic.wizard.power.title")} density="compact">
          <thead>
            <tr>
              {columns.map((c) => (
                <th scope="col" key={c}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {cells.map((row, i) => (
              <tr key={i}>
                {row.map((cell, j) => (
                  <td className="ds-num" key={j}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}

export function WizardCampaignConfirm({
  form,
  setForm,
  policies,
  api = wizardApi,
  onStarted,
  debounceMs = DEBOUNCE_MS,
}: {
  form: WizardForm;
  setForm: SetForm;
  policies: PoliciesResponse | null;
  api?: Pick<WizardApi, "createJob" | "planCampaign" | "startCampaign">;
  onStarted: (campaignId: string) => void;
  debounceMs?: number;
}) {
  const { t } = useLocale();
  const jobs = useRef(new JobAttempts());
  const keys = useRef(new IntentKeys());
  const alive = useRef(true);
  const [plan, setPlan] = useState<CampaignPlan | null>(null);
  const [busy, setBusy] = useState<"plan" | "start" | null>("plan");
  const [failure, setFailure] = useState<string | null>(null);
  const [startFailure, setStartFailure] = useState<string | null>(null);
  const planned = useRef("");

  const problems = validateCampaign(form);
  const schedule = SCHEDULE_KINDS.find((k) => k.value === form.scheduleKind);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const requestKey = JSON.stringify([
    buildJobDraft(form, policies),
    form.armIds,
    form.referenceId,
    form.scheduleKind,
    form.segmentTrials,
    form.seed,
    form.labelBasis,
    form.alpha,
    form.preregistered,
    form.campaignMode,
  ]);

  const runPlan = useCallback(async () => {
    if (hasProblems(validateCampaign(form))) {
      setBusy(null);
      return;
    }
    setBusy("plan");
    setFailure(null);
    const key = requestKey;
    try {
      const jobId = await jobs.current.ensure(
        api,
        buildJobDraft(form, policies),
      );
      const next = await api.planCampaign(buildCampaignRequest(form, jobId));
      if (!alive.current || key !== planned.current) return;
      setPlan(next);
    } catch (error) {
      if (alive.current && key === planned.current)
        setFailure(failureText(error, t));
    } finally {
      if (alive.current && key === planned.current) setBusy(null);
    }
  }, [api, form, policies, requestKey, t]);

  useEffect(() => {
    planned.current = requestKey;
    setPlan(null);
    const timer = setTimeout(() => void runPlan(), debounceMs);
    return () => clearTimeout(timer);
    // The plan follows the request, not the callback's identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requestKey, debounceMs]);

  const start = async () => {
    if (!plan || busy) return;
    const hash = plan.plan_sha256 ?? plan.campaign_sha256;
    const intent = `start:${hash}:${requestKey}`;
    setBusy("start");
    setStartFailure(null);
    try {
      const jobId = await jobs.current.ensure(
        api,
        buildJobDraft(form, policies),
      );
      const { campaign_id } = await api.startCampaign(
        buildCampaignRequest(form, jobId),
        hash,
        keys.current.idFor(intent),
      );
      keys.current.release(intent);
      if (alive.current) onStarted(campaign_id);
    } catch (error) {
      if (alive.current) {
        setStartFailure(failureText(error, t));
        if (error instanceof ApiError && error.status !== 0)
          keys.current.release(intent);
      }
    } finally {
      if (alive.current) setBusy(null);
    }
  };

  const canStart =
    plan !== null && plan.refusals.length === 0 && !hasProblems(problems);
  const reason = !plan
    ? undefined
    : plan.refusals.length > 0
      ? t("automatic.wizard.plan.not_launchable")
      : hasProblems(problems)
        ? t("automatic.wizard.err.fix_form")
        : undefined;

  return (
    <div className="aw-form">
      <div className="aw-grid">
        <Field
          label={t("automatic.wizard.campaign.trials")}
          hint={t("automatic.wizard.campaign.trials_hint")}
          error={problems.trials ? t(problems.trials) : undefined}
        >
          <Input
            inputMode="numeric"
            value={form.trialsPerArm}
            onChange={(e) => setForm({ trialsPerArm: e.target.value })}
          />
        </Field>
        <Field
          label={t("automatic.wizard.campaign.schedule")}
          error={undefined}
        >
          <Select
            value={form.scheduleKind}
            onChange={(e) =>
              setForm({
                scheduleKind: e.target.value as WizardForm["scheduleKind"],
              })
            }
          >
            {SCHEDULE_KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                {t(k.key)}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label={t("automatic.wizard.campaign.segment")}
          hint={t("automatic.wizard.campaign.segment_hint")}
          error={problems.segment ? t(problems.segment) : undefined}
        >
          <Input
            inputMode="numeric"
            value={form.segmentTrials}
            onChange={(e) => setForm({ segmentTrials: e.target.value })}
          />
        </Field>
        <Field
          label={t("automatic.wizard.campaign.seed")}
          error={problems.seed ? t(problems.seed) : undefined}
        >
          <Input
            inputMode="numeric"
            value={form.seed}
            onChange={(e) => setForm({ seed: e.target.value })}
          />
        </Field>
        <Field label={t("automatic.wizard.campaign.basis")}>
          <Select
            value={form.labelBasis}
            onChange={(e) => setForm({ labelBasis: e.target.value })}
          >
            {LABEL_BASES.map((b) => (
              <option key={b.value} value={b.value}>
                {t(b.key)}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label={t("automatic.wizard.campaign.alpha")}
          error={problems.alpha ? t(problems.alpha) : undefined}
        >
          <Input
            inputMode="decimal"
            value={form.alpha}
            onChange={(e) => setForm({ alpha: e.target.value })}
          />
        </Field>
      </div>
      {schedule?.exploratory && (
        <Note tone="warning">{t("automatic.wizard.campaign.exploratory")}</Note>
      )}
      <RadioGroup legend={t("automatic.wizard.campaign.mode")}>
        <div className="aw-modes">
          <div>
            <Radio
              name="aw-campaign-mode"
              label={t("automatic.wizard.campaign.mode_guided")}
              checked={form.campaignMode === "guided"}
              onChange={() => setForm({ campaignMode: "guided" })}
            />
            <p className="aw-mode__reason">
              {t("automatic.wizard.campaign.mode_guided_note")}
            </p>
          </div>
          <div>
            <Radio
              name="aw-campaign-mode"
              label={t("automatic.wizard.campaign.mode_dry_run")}
              checked={form.campaignMode === "dry_run"}
              onChange={() => setForm({ campaignMode: "dry_run" })}
            />
            <p className="aw-mode__reason">
              {t("automatic.wizard.campaign.mode_dry_run_note")}
            </p>
          </div>
        </div>
      </RadioGroup>
      <Switch
        label={t("automatic.wizard.campaign.prereg")}
        description={t("automatic.wizard.campaign.prereg_note")}
        checked={form.preregistered}
        onChange={(e) => setForm({ preregistered: e.target.checked })}
      />

      {busy === "plan" && (
        <div role="status" aria-label={t("automatic.wizard.plan.making")}>
          <Skeleton height={96} />
        </div>
      )}
      {failure && (
        <RequestProblem
          action="automatic.wizard.plan.failed"
          message={failure}
        />
      )}
      {plan && (
        <section
          className="aw-plan"
          aria-label={t("automatic.wizard.campaign.plan_title")}
        >
          <dl className="pg-summary">
            <div>
              <dt>{t("automatic.wizard.campaign.hash")}</dt>
              <dd>
                <code>{plan.campaign_sha256.slice(0, 16)}</code>
              </dd>
            </div>
            <div>
              <dt>{t("automatic.wizard.campaign.settings_hash")}</dt>
              <dd>
                <code>{plan.settings_sha256.slice(0, 16)}</code>
              </dd>
            </div>
            <div>
              <dt>{t("automatic.wizard.campaign.segments")}</dt>
              <dd>{plan.segments.length}</dd>
            </div>
            <div>
              <dt>{t("automatic.wizard.campaign.switches")}</dt>
              <dd>
                {plan.switches} × {t("automatic.wizard.campaign.switch_time")}{" "}
                <Badge tone="neutral">
                  {t("automatic.wizard.campaign.unmeasured")}
                </Badge>
              </dd>
            </div>
          </dl>
          <PowerTable power={plan.power} />
          <ChecksList checks={plan.checks} />
          <Refusals codes={plan.refusals} />
        </section>
      )}
      {startFailure && (
        <RequestProblem
          action="automatic.wizard.campaign.start_failed"
          message={startFailure}
        />
      )}
      {plan && (
        <PhraseConfirm
          phrase="start-campaign"
          label={t("automatic.wizard.campaign.type")}
          hint={t("automatic.wizard.campaign.type_hint")}
          buttonLabel={t("automatic.wizard.campaign.start")}
          busy={busy === "start"}
          disabled={!canStart || busy === "plan"}
          disabledReason={reason}
          onConfirm={() => void start()}
        />
      )}
    </div>
  );
}
