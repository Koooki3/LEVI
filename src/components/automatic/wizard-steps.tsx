"use client";
// Steps 2 and 3 of the wizard: which policy is evaluated and how the scene is
// reset, then the task and its limits. Shared by the single run and the
// multi-model plan; only the policy picker differs.
import {
  Badge,
  Checkbox,
  Field,
  Input,
  Radio,
  RadioGroup,
  Switch,
  Textarea,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { EXECUTION_MODE_LABEL } from "./run-logic";
import type { ExecutionMode } from "./types";
import {
  HUMAN_RESET,
  INSTRUCTION_MAX,
  attestAvailable,
  MAX_ARMS,
  forwardCheckpoints,
  resetOptions,
  shortRunWarning,
  type Problems,
  type WizardForm,
} from "./wizard-logic";
import type {
  Capabilities,
  PoliciesResponse,
  PolicyCheckpoint,
} from "./wizard-types";

type SetForm = (change: Partial<WizardForm>) => void;

const HASH_KEYS = {
  verified: "automatic.wizard.hash.verified",
  recorded: "automatic.wizard.hash.recorded",
  none: "automatic.wizard.hash.none",
} as const;

function CheckpointFacts({ checkpoint }: { checkpoint: PolicyCheckpoint }) {
  const { t } = useLocale();
  return (
    <>
      <p className="aw-choice__meta">
        <span>
          {t("automatic.wizard.policy.config")}:{" "}
          <code>{checkpoint.config}</code>
        </span>
        {checkpoint.family && (
          <span>
            {t("automatic.wizard.policy.family")}: {checkpoint.family}
          </span>
        )}
        <Badge
          tone={
            checkpoint.sha256_status === "verified"
              ? "success"
              : checkpoint.sha256_status === "recorded"
                ? "info"
                : "warning"
          }
        >
          {t(HASH_KEYS[checkpoint.sha256_status])}
        </Badge>
      </p>
      {checkpoint.notes.length > 0 && (
        <ul className="aw-choice__notes">
          {checkpoint.notes.map((note, index) => (
            <li key={index}>{note}</li>
          ))}
        </ul>
      )}
    </>
  );
}

function ProblemLine({ problems, name }: { problems: Problems; name: string }) {
  const { t } = useLocale();
  return problems[name] ? (
    <p className="ds-field__error" role="alert">
      {t(problems[name])}
    </p>
  ) : null;
}

export function StepChoose({
  form,
  setForm,
  policies,
  problems,
}: {
  form: WizardForm;
  setForm: SetForm;
  policies: PoliciesResponse | null;
  problems: Problems;
}) {
  const { t } = useLocale();
  const forward = forwardCheckpoints(policies);
  const resets = resetOptions(policies);
  const toggleArm = (id: string, on: boolean) => {
    const armIds = on
      ? [...form.armIds, id]
      : form.armIds.filter((x) => x !== id);
    setForm({
      armIds,
      referenceId: armIds.includes(form.referenceId) ? form.referenceId : "",
    });
  };
  return (
    <div className="aw-form">
      <RadioGroup
        legend={
          form.mode === "single"
            ? t("automatic.wizard.policy.pick_one")
            : t("automatic.wizard.policy.pick_many").replace(
                "{max}",
                String(MAX_ARMS),
              )
        }
      >
        {forward.length === 0 && (
          <p className="pg-pool-hint">{t("automatic.wizard.policy.none")}</p>
        )}
        <ul className="aw-choices">
          {forward.map((checkpoint) => {
            const selected =
              form.mode === "single"
                ? form.policyId === checkpoint.id
                : form.armIds.includes(checkpoint.id);
            return (
              <li
                key={checkpoint.id}
                className="aw-choice"
                data-selected={selected}
              >
                {form.mode === "single" ? (
                  <Radio
                    name="aw-policy"
                    label={checkpoint.name}
                    checked={selected}
                    onChange={() => setForm({ policyId: checkpoint.id })}
                  />
                ) : (
                  <Checkbox
                    label={checkpoint.name}
                    checked={selected}
                    onChange={(event) =>
                      toggleArm(checkpoint.id, event.target.checked)
                    }
                  />
                )}
                <CheckpointFacts checkpoint={checkpoint} />
                {form.mode === "campaign" && selected && (
                  <Radio
                    name="aw-reference"
                    label={t("automatic.wizard.policy.is_reference")}
                    description={t("automatic.wizard.policy.reference_note")}
                    checked={form.referenceId === checkpoint.id}
                    onChange={() => setForm({ referenceId: checkpoint.id })}
                  />
                )}
              </li>
            );
          })}
        </ul>
        <ProblemLine problems={problems} name="policy" />
        <ProblemLine problems={problems} name="arms" />
        <ProblemLine problems={problems} name="reference" />
      </RadioGroup>

      <RadioGroup legend={t("automatic.wizard.reset.legend")}>
        <ul className="aw-choices">
          <li
            className="aw-choice"
            data-selected={form.resetChoice === HUMAN_RESET}
          >
            <Radio
              name="aw-reset"
              label={t("automatic.wizard.reset.human")}
              description={t("automatic.wizard.reset.human_note")}
              checked={form.resetChoice === HUMAN_RESET}
              onChange={() => setForm({ resetChoice: HUMAN_RESET })}
            />
          </li>
          {resets.resetCheckpoints.map((checkpoint) => (
            <li
              key={checkpoint.id}
              className="aw-choice"
              data-selected={form.resetChoice === checkpoint.id}
            >
              <Radio
                name="aw-reset"
                label={`${t("automatic.wizard.reset.policy")}: ${checkpoint.name}`}
                checked={form.resetChoice === checkpoint.id}
                onChange={() => setForm({ resetChoice: checkpoint.id })}
              />
              <CheckpointFacts checkpoint={checkpoint} />
            </li>
          ))}
        </ul>
        {resets.onlyHumanReasonKey && (
          <p className="pg-pool-hint" role="note">
            {t(resets.onlyHumanReasonKey)}
          </p>
        )}
        <ProblemLine problems={problems} name="reset" />
      </RadioGroup>

      {form.resetChoice === HUMAN_RESET && (
        <div>
          <Switch
            label={t("automatic.wizard.reset.attested")}
            description={t("automatic.wizard.reset.attested_note")}
            checked={attestAvailable() && form.operatorAttested}
            disabled={!attestAvailable()}
            onChange={(event) =>
              setForm({ operatorAttested: event.target.checked })
            }
          />
          {!attestAvailable() && (
            <p className="pg-pool-hint" role="note">
              {t("automatic.wizard.reset.attested_unavailable")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function StepSettings({
  form,
  setForm,
  capabilities,
  problems,
}: {
  form: WizardForm;
  setForm: SetForm;
  capabilities: Capabilities | null;
  problems: Problems;
}) {
  const { t } = useLocale();
  const modes = capabilities?.modes ?? { dry_run: { available: true } };
  const names = Object.keys(modes);
  if (!names.includes("dry_run")) names.unshift("dry_run");
  return (
    <div className="aw-form">
      <Field
        label={t("automatic.wizard.settings.instruction")}
        hint={t("automatic.wizard.settings.instruction_hint").replace(
          "{max}",
          String(INSTRUCTION_MAX),
        )}
        error={problems.instruction ? t(problems.instruction) : undefined}
        required
      >
        <Textarea
          value={form.instruction}
          rows={3}
          onChange={(event) => setForm({ instruction: event.target.value })}
        />
      </Field>
      <div className="aw-grid">
        <Field
          label={t("automatic.wizard.settings.max_steps")}
          hint={t("automatic.wizard.settings.max_steps_hint")}
          error={problems.maxSteps ? t(problems.maxSteps) : undefined}
          required
        >
          <Input
            inputMode="numeric"
            value={form.maxSteps}
            onChange={(event) => setForm({ maxSteps: event.target.value })}
          />
        </Field>
        {form.mode === "single" && (
          <Field
            label={t("automatic.wizard.settings.episodes")}
            hint={t("automatic.wizard.settings.episodes_hint")}
            error={problems.episodes ? t(problems.episodes) : undefined}
            required
          >
            <Input
              inputMode="numeric"
              value={form.episodes}
              onChange={(event) => setForm({ episodes: event.target.value })}
            />
          </Field>
        )}
      </div>
      {shortRunWarning(form) && (
        <p className="aw-warning" role="note">
          {t("automatic.wizard.settings.short_run")}
        </p>
      )}
      <Switch
        label={t("automatic.wizard.settings.early_stop")}
        description={t("automatic.wizard.settings.early_stop_note")}
        checked={form.allowEarlyStop}
        onChange={(event) => setForm({ allowEarlyStop: event.target.checked })}
      />
      {form.mode === "single" && (
        <RadioGroup legend={t("automatic.wizard.settings.mode")}>
          <div className="aw-modes">
            {names.map((name) => {
              const entry = modes[name];
              const available = Boolean(entry?.available);
              return (
                <div key={name}>
                  <Radio
                    name="aw-exec-mode"
                    label={
                      name === "dry_run"
                        ? t("automatic.wizard.settings.mode_dry_run")
                        : EXECUTION_MODE_LABEL[name as ExecutionMode]
                          ? t(EXECUTION_MODE_LABEL[name as ExecutionMode])
                          : name
                    }
                    checked={form.executionMode === name}
                    disabled={!available}
                    onChange={() => setForm({ executionMode: name })}
                  />
                  {!available && (
                    <p className="aw-mode__reason">
                      {entry?.reason ??
                        t("automatic.wizard.settings.mode_unavailable")}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
          <Note tone="info">{t("automatic.wizard.settings.mode_note")}</Note>
        </RadioGroup>
      )}
    </div>
  );
}
