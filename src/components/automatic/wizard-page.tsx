"use client";
// The evaluation wizard (/automatic/new): environment, choice, settings,
// confirm. A single run goes to /automatic/runs/<id> after launch; a
// multi-model plan goes to /automatic/campaigns/<id>.
import { useCallback, useEffect, useState } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { Button, SegmentedControl, Skeleton } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, RequestProblem } from "@/components/pages-ui/feedback";
import { SetupGuideLoader } from "./setup-guide";
import { wizardApi, type WizardApi } from "./wizard-api";
import { WizardCampaignConfirm } from "./wizard-campaign";
import {
  defaultForm,
  hasProblems,
  validateChoice,
  validateSettings,
  type WizardForm,
  type WizardMode,
} from "./wizard-logic";
import { WizardRunConfirm } from "./wizard-run";
import { StepChoose, StepSettings } from "./wizard-steps";
import type { Capabilities, PoliciesResponse } from "./wizard-types";

const STEP_KEYS = [
  "automatic.wizard.step.environment",
  "automatic.wizard.step.choose",
  "automatic.wizard.step.settings",
  "automatic.wizard.step.confirm",
] as const;

export function WizardPage({
  api = wizardApi,
  navigate,
}: {
  api?: WizardApi;
  /** Go to a path (the page passes the router's push). */
  navigate: (href: string) => void;
}) {
  const { t } = useLocale();
  const [step, setStep] = useState(0);
  const [form, setFormState] = useState<WizardForm>(() => defaultForm());
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [policies, setPolicies] = useState<PoliciesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempted, setAttempted] = useState(false);

  const setForm = useCallback(
    (change: Partial<WizardForm>) =>
      setFormState((prev) => ({ ...prev, ...change })),
    [],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [caps, pols] = await Promise.all([
        api.getCapabilities(),
        api.getPolicies(),
      ]);
      setCapabilities(caps);
      setPolicies(pols);
      // Defaults from the machine, only for fields the person has not typed.
      setFormState((prev) =>
        prev.instruction
          ? prev
          : {
              ...prev,
              maxSteps: String(caps.defaults.max_steps),
              episodes: String(caps.defaults.episodes),
            },
      );
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, [api]);
  useEffect(() => {
    void load();
  }, [load]);

  const choiceProblems = validateChoice(form, policies);
  const settingsProblems = validateSettings(form);

  const next = () => {
    if (step === 1 && hasProblems(choiceProblems)) return setAttempted(true);
    if (step === 2 && hasProblems(settingsProblems)) return setAttempted(true);
    setAttempted(false);
    setStep((s) => Math.min(s + 1, 3));
  };
  const back = () => {
    setAttempted(false);
    setStep((s) => Math.max(s - 1, 0));
  };

  return (
    <>
      <header className="pg-head">
        <h1>{t("automatic.wizard.title")}</h1>
      </header>
      <p className="pg-pool-hint">{t("automatic.wizard.intro")}</p>
      <SegmentedControl
        label={t("automatic.wizard.mode.label")}
        value={form.mode}
        onChange={(mode) => {
          setForm({ mode: mode as WizardMode });
          if (step > 1) setStep(1);
        }}
        options={[
          { value: "single", label: t("automatic.wizard.mode.single") },
          { value: "campaign", label: t("automatic.wizard.mode.campaign") },
        ]}
      />
      <ol className="aw-stepper" aria-label={t("automatic.wizard.steps")}>
        {STEP_KEYS.map((key, index) => (
          <li
            key={key}
            aria-current={index === step ? "step" : undefined}
            data-done={index < step}
          >
            {index + 1}. {t(key)}
          </li>
        ))}
      </ol>

      <section className="aw-panel" aria-labelledby="aw-step-title">
        <h2 id="aw-step-title">{t(STEP_KEYS[step])}</h2>
        {step === 0 && <SetupGuideLoader load={api.getSetupGuide} />}
        {step > 0 && step < 3 && loading && <Skeleton height={96} />}
        {step > 0 && loadError && (
          <RequestProblem
            action="automatic.wizard.load_failed"
            message={loadError}
            onRetry={() => void load()}
          />
        )}
        {step === 1 && !loading && !loadError && (
          <StepChoose
            form={form}
            setForm={setForm}
            policies={policies}
            problems={attempted ? choiceProblems : {}}
          />
        )}
        {step === 2 && !loading && !loadError && (
          <StepSettings
            form={form}
            setForm={setForm}
            capabilities={capabilities}
            problems={attempted ? settingsProblems : {}}
          />
        )}
        {step === 3 &&
          (form.mode === "single" ? (
            <WizardRunConfirm
              form={form}
              policies={policies}
              api={api}
              onLaunched={(id) =>
                navigate(`/automatic/runs/${encodeURIComponent(id)}`)
              }
            />
          ) : (
            <WizardCampaignConfirm
              form={form}
              setForm={setForm}
              policies={policies}
              api={api}
              onStarted={(id) =>
                navigate(`/automatic/campaigns/${encodeURIComponent(id)}`)
              }
            />
          ))}
        {attempted && step > 0 && step < 3 && (
          <Note tone="warning" role="alert">
            {t("automatic.wizard.err.fix_form")}
          </Note>
        )}
        <div className="aw-actions">
          <Button icon={ArrowLeft} disabled={step === 0} onClick={back}>
            {t("automatic.wizard.back")}
          </Button>
          {step < 3 && (
            <Button
              variant="primary"
              iconEnd={ArrowRight}
              disabled={step > 0 && (loading || loadError !== null)}
              onClick={next}
            >
              {t("automatic.wizard.next")}
            </Button>
          )}
        </div>
      </section>
    </>
  );
}
