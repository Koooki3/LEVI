"use client";
// The Client command card of the marvel0: a form for the demo client's
// run (task instruction, steps, eval number, lowest height, RTC horizon, group,
// note) and the exact command for it. LEVI's backend validates the form and
// renders the command (levi/marvel0/command.py); every flag that guards the arm
// comes from the site file and is listed here, never editable. The model, RTC
// and the guidance strength are not set here: they are the page's one selection
// (selection.ts), chosen in the policy server card, so the command cannot ask
// for another model than the server serves. A person copies the command into a
// terminal, or starts the client from the Run card of this step, with a hand
// on the physical e-stop; this card itself starts nothing. The voice card
// above the task instruction (speech-panel.tsx) only proposes one of the
// allowed instructions; pressing its Use fills the task instruction and does
// nothing else.
import { useMemo, useRef, useState, useEffect, type ReactNode } from "react";
import { Check, ClipboardCopy, TriangleAlert } from "lucide-react";
import { Badge, Button, Card, Field, Input, Select } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { Refusal, WordedText } from "./action-parts";
import { COMMAND_ANCHOR } from "./chain-logic";
import { wordOf } from "./ctl-text";
import {
  DEFAULT_HZ,
  bodyKey,
  commandBody,
  initialCommandForm,
  placeholders,
  rateFacts,
  rateOf,
  readPreview,
  serverMatch,
  warningSay,
  commandFormFromRun,
  type CommandForm,
  type FieldName,
  type Preview,
} from "./command-logic";
import type { ControlFacts, ControlView } from "./control-logic";
import type { ApiFailure, Marvel0Api } from "./deploy-api";
import type { DeployStatus } from "./deploy-logic";
import { fieldMessages } from "./marvel0-failure";
import type { Say } from "./marvel0-logic";
import { sayText } from "./marvel0-say";
import type { RunView } from "./run-logic";
import { RunStart } from "./run-start";
import type { Effective } from "./selection";
import type { RunActions } from "./use-run-actions";
import { SpeechPanel } from "./speech-panel";
import type { Marvel0Catalog } from "./use-marvel0-actions";

interface Made {
  preview: Preview;
  /** The request it was made for: a form that differs has outgrown it. */
  key: string;
}

export function CommandPanel({
  api,
  catalog,
  status,
  selection,
  onUseAnother,
  run = null,
  runActions,
  resumeBody = null,
}: {
  api: Marvel0Api;
  catalog: Marvel0Catalog;
  /** The policy server's state, to say whether it fits the command. */
  status: DeployStatus | null;
  /** The page's one selected model, RTC and guidance strength; the form
   * follows it. Null before the model list is read. */
  selection: Effective | null;
  /** Takes the person to the policy server card's model control. */
  onUseAnother: () => void;
  /** The hosted client's state and what its buttons do: with them the result
   * offers "Start the client" next to Copy. */
  run?: RunView | null;
  runActions?: RunActions;
  /** Saved body of an ended run with only its prompt replaced. */
  resumeBody?: Record<string, unknown> | null;
}) {
  const { t } = useLocale();
  const models = catalog.models;
  const site = catalog.site;
  const selectedId = selection?.modelId ?? "";
  const initial = useMemo(
    () =>
      models && site ? initialCommandForm(models, site, selectedId) : null,
    [models, site, selectedId],
  );
  const [edits, setEdits] = useState<Partial<CommandForm>>({});
  const [made, setMade] = useState<Made | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [local, setLocal] = useState<Partial<Record<FieldName, Say>>>({});
  const [pending, setPending] = useState(false);
  const [resumeBodyKey, setResumeBodyKey] = useState<string | null>(null);
  const [resumePrompt, setResumePrompt] = useState<string | null>(null);
  const [resumeMode, setResumeMode] = useState(false);
  const busy = useRef(false);
  const taskInput = useRef<HTMLInputElement>(null);
  const previousResume = useRef<{ form: CommandForm; prompt: string } | null>(
    null,
  );
  const savedForm = resumeBody ? commandFormFromRun(resumeBody) : null;
  const savedKey = resumeBody ? bodyKey(resumeBody) : null;
  useEffect(() => {
    if (savedForm && savedKey && resumeBodyKey !== savedKey) {
      setResumeBodyKey(savedKey);
      setResumePrompt(savedForm.prompt);
      setResumeMode(true);
      previousResume.current = { form: savedForm, prompt: savedForm.prompt };
      setMade(null);
      setFailure(null);
      setLocal({});
      taskInput.current?.focus();
    } else if (!resumeBody && previousResume.current) {
      const saved = previousResume.current;
      setEdits({ ...saved.form, prompt: saved.prompt });
      setResumeMode(false);
      setResumeBodyKey(null);
      setResumePrompt(null);
      previousResume.current = null;
    }
  }, [savedForm, savedKey, resumeBodyKey]);
  if (!models || !site || !initial || !selection) return null;

  // A resumed run is pinned to its saved form. Only the prompt remains editable;
  // its request body carries every other field verbatim from that run.
  const resuming =
    resumeMode && savedForm !== null && savedKey === resumeBodyKey;
  const form: CommandForm = resuming
    ? { ...savedForm, prompt: resumePrompt ?? savedForm.prompt }
    : {
        ...initial,
        ...edits,
        modelId: selection.modelId,
        rtc: selection.rtc,
      };
  const edit = (change: Partial<CommandForm>) =>
    setEdits((cur) => ({ ...cur, ...change }));
  const model = models.models.find((m) => m.id === form.modelId) ?? null;
  const hints = placeholders(form, models, site);
  const control = site.control;
  const facts = rateFacts(form, models, site);
  const built = commandBody(form, control?.default ?? DEFAULT_HZ);
  const body =
    resuming && resumeBody
      ? { ...resumeBody, prompt: form.prompt }
      : built.body;
  const errors = built.errors;
  const key = bodyKey(body);
  const stale = made !== null && made.key !== key;
  const server = serverMatch({ modelId: form.modelId, rtc: form.rtc }, status);
  const remote: Partial<
    Record<string, ReturnType<typeof fieldMessages>[string]>
  > = failure ? fieldMessages(failure, { site }) : {};
  const fieldError = (name: FieldName) => {
    const own = local[name];
    if (own) return sayText(own, t);
    const word = remote[name];
    if (!word) return undefined;
    return word.say ? sayText(word.say, t) : word.text;
  };
  const otherFailure =
    failure && Object.keys(remote).length === 0 ? failure : null;

  async function make() {
    if (busy.current || !model) return;
    setLocal(errors);
    if (Object.keys(errors).length > 0) return;
    busy.current = true;
    setPending(true);
    setFailure(null);
    let answer;
    try {
      answer = await api.preview(body);
    } finally {
      busy.current = false;
      setPending(false);
    }
    if (!answer.ok) {
      setFailure(answer);
      return;
    }
    const preview = readPreview(answer.data);
    if (!preview) {
      setFailure({
        ok: false,
        status: answer.status,
        code: "format",
        message: "",
        extra: {},
        field: null,
        fields: [],
      });
      return;
    }
    setMade({ preview, key });
  }

  return (
    <Card
      padding="compact"
      className="rg-card rg-action rg-card--unknown"
      id={COMMAND_ANCHOR}
      tabIndex={-1}
      data-testid="marvel0-command"
      title={t("marvel0.cmd.title")}
      description={t("marvel0.cmd.scope")}
    >
      {resuming && (
        <div className="rg-actions-row" data-testid="marvel0-command-resumed">
          <p className="rg-muted" role="status">
            {t("marvel0.episodeChange.promptOnly")}
          </p>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              if (!savedForm) return;
              const prompt = resumePrompt ?? savedForm.prompt;
              setEdits({ ...savedForm, prompt });
              setResumeMode(false);
              previousResume.current = null;
            }}
            data-testid="marvel0-command-edit-parameters"
          >
            {t("marvel0.episodeChange.editAll")}
          </Button>
        </div>
      )}
      <form
        className="rg-form rg-form--grid"
        onSubmit={(event) => {
          event.preventDefault();
          void make();
        }}
      >
        {models.models.length === 0 ? (
          <p className="rg-bad">{t("marvel0.deploy.form.no_models")}</p>
        ) : (
          <>
            <Follow
              selection={selection}
              error={fieldError("model_id")}
              onUseAnother={onUseAnother}
            />
            {!resuming && (
              <SpeechPanel
                modelId={form.modelId}
                onUse={(prompt) => {
                  edit({ prompt });
                  taskInput.current?.focus();
                }}
              />
            )}
            <Field
              className="rg-span"
              label={t("marvel0.cmd.field.prompt")}
              hint={
                <>
                  {sayText(
                    {
                      key: "marvel0.cmd.field.prompt_hint",
                      params: { n: site.caps.promptMaxChars },
                    },
                    t,
                  )}
                  {model?.restrictedPrompts && (
                    <> {t("marvel0.deploy.model.restricted")}.</>
                  )}
                </>
              }
              error={fieldError("prompt")}
            >
              <Input
                ref={taskInput}
                value={form.prompt}
                list="marvel0-known-prompts"
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                lang="en"
                onChange={(event) => {
                  if (resuming) {
                    setResumePrompt(event.target.value);
                    if (previousResume.current)
                      previousResume.current.prompt = event.target.value;
                  } else edit({ prompt: event.target.value });
                }}
              />
            </Field>
            <datalist id="marvel0-known-prompts">
              {(model?.restrictedPrompts
                ? site.prompts.restricted
                : site.prompts.known
              ).map((prompt) => (
                <option key={prompt} value={prompt} />
              ))}
            </datalist>
            <Field
              label={t("marvel0.cmd.field.max_steps")}
              hint={sayText(
                {
                  key: "marvel0.cmd.field.max_steps_hint",
                  params: { cap: site.caps.maxSteps.cap },
                },
                t,
              )}
              error={fieldError("max_steps")}
            >
              <Input
                value={form.maxSteps}
                disabled={resuming}
                inputMode="numeric"
                autoComplete="off"
                onChange={(event) => edit({ maxSteps: event.target.value })}
              />
            </Field>
            <Field
              label={t("marvel0.cmd.field.eval_num")}
              hint={sayText(
                {
                  key: "marvel0.cmd.field.eval_num_hint",
                  params: { cap: site.caps.evalNum.cap },
                },
                t,
              )}
              error={fieldError("eval_num")}
            >
              <Input
                value={form.evalNum}
                disabled={resuming}
                inputMode="numeric"
                autoComplete="off"
                onChange={(event) => edit({ evalNum: event.target.value })}
              />
            </Field>
            <Field
              label={t("marvel0.cmd.field.min_z")}
              hint={sayText(
                {
                  key: "marvel0.cmd.field.min_z_hint",
                  params: { floor: site.minZ.floor, home: site.homeZ },
                },
                t,
              )}
              error={fieldError("min_z")}
            >
              <Input
                value={form.minZ}
                disabled={resuming}
                inputMode="decimal"
                autoComplete="off"
                onChange={(event) => edit({ minZ: event.target.value })}
              />
            </Field>
            <Field
              label={t(
                form.rtc
                  ? "marvel0.cmd.field.horizon_rtc"
                  : "marvel0.cmd.field.horizon_open",
              )}
              hint={sayText(
                {
                  key: "marvel0.cmd.field.horizon_hint",
                  params: { max: hints.horizonMax, default: hints.horizon },
                },
                t,
              )}
              error={fieldError("rtc_horizon")}
            >
              <Input
                value={form.horizon}
                disabled={resuming}
                inputMode="numeric"
                autoComplete="off"
                placeholder={hints.horizon}
                onChange={(event) => edit({ horizon: event.target.value })}
              />
            </Field>
            {control && facts && (
              <Rate
                control={control}
                facts={facts}
                value={form.hz}
                disabled={resuming}
                lowered={hints.horizonLoweredFrom}
                horizon={hints.horizon}
                error={fieldError("control_hz")}
                onChange={(hz) => edit({ hz })}
              />
            )}
            <Field
              className="rg-span"
              label={t("marvel0.cmd.field.group")}
              hint={t("marvel0.cmd.field.group_hint")}
              error={fieldError("rollout_group")}
            >
              <Input
                value={form.group}
                disabled={resuming}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                lang="en"
                placeholder={hints.group}
                onChange={(event) => edit({ group: event.target.value })}
              />
            </Field>
            <Field
              className="rg-span"
              label={t("marvel0.cmd.field.note")}
              hint={sayText(
                {
                  key: "marvel0.cmd.field.note_hint",
                  params: { n: site.caps.noteMaxChars },
                },
                t,
              )}
              error={fieldError("note")}
            >
              <Input
                value={form.note}
                disabled={resuming}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                onChange={(event) => edit({ note: event.target.value })}
              />
            </Field>
          </>
        )}
        <details className="rg-details rg-span">
          <summary>{t("marvel0.cmd.fixed.title")}</summary>
          <p className="rg-muted">{t("marvel0.cmd.fixed.hint")}</p>
          <ul
            className="rg-notes"
            data-testid="marvel0-command-fixed"
            lang="en"
          >
            {site.fixedFlags.map((flag) => (
              <li key={flag}>
                <code>{flag}</code>
              </li>
            ))}
          </ul>
        </details>
        <div className="rg-actions-row rg-span">
          <Button
            type="submit"
            variant="primary"
            loading={pending}
            disabled={!model}
          >
            {t("marvel0.cmd.btn.make")}
          </Button>
        </div>
      </form>

      <p
        className={server.good ? "rg-muted" : "rg-warn"}
        data-testid="marvel0-command-server"
        data-kind={server.kind}
      >
        {sayText(server.say, t)}
      </p>

      {otherFailure && (
        <Refusal
          testId="marvel0-command-problem"
          title="marvel0.cmd.result.failed"
          failure={otherFailure}
          context={{ site }}
          worded={
            otherFailure.code === "format"
              ? { say: { key: "marvel0.act.models_format" }, text: "" }
              : undefined
          }
        />
      )}

      {made && (
        <CommandResult
          preview={made.preview}
          stale={stale}
          modelLabel={made.preview.needs.modelId}
          start={
            runActions ? (
              <RunStart
                view={run}
                actions={runActions}
                form={body}
                group={form.group.trim() || hints.group}
                modelId={form.modelId}
                rtc={form.rtc}
                hz={control ? rateOf(form) : null}
                referenceHz={control?.referenceHz ?? DEFAULT_HZ}
                stale={stale}
              />
            ) : null
          }
        />
      )}
    </Card>
  );
}

/** The control rate: a select of the site's options, the numbers the rate
 * changes (worked out here the way the backend works them out), and, for a
 * rate other than the one the policy was trained at, the note that it is
 * untested. */
function Rate({
  control,
  facts,
  value,
  disabled = false,
  lowered,
  horizon,
  error,
  onChange,
}: {
  control: ControlView;
  facts: ControlFacts;
  value: string;
  disabled?: boolean;
  /** The site's RTC horizon default when this rate lowers it. */
  lowered: number | null;
  /** What an empty horizon field means at this rate. */
  horizon: string;
  error?: string;
  onChange: (hz: string) => void;
}) {
  const { t } = useLocale();
  const rows: Array<[string, string, string]> = [
    [
      "period",
      t("marvel0.hz.fact.period"),
      sayText(
        { key: "marvel0.hz.value.period", params: { ms: facts.periodMs } },
        t,
      ),
    ],
    [
      "episode",
      t("marvel0.hz.fact.episode"),
      sayText(
        {
          key: "marvel0.hz.value.episode",
          params: { steps: facts.steps, seconds: facts.episodeS },
        },
        t,
      ),
    ],
    [
      "step_caps",
      t("marvel0.hz.fact.step_caps"),
      sayText(
        {
          key: "marvel0.hz.value.step_caps",
          params: { m: facts.stepCapM, rad: facts.stepCapRad },
        },
        t,
      ),
    ],
    [
      "speed_caps",
      t("marvel0.hz.fact.speed_caps"),
      sayText(
        {
          key: "marvel0.hz.value.speed_caps",
          params: { ms: facts.speedCapMS, rads: facts.speedCapRadS },
        },
        t,
      ),
    ],
    [
      "chunk",
      t("marvel0.hz.fact.chunk"),
      sayText(
        {
          key: "marvel0.hz.value.chunk",
          params: { n: control.actionHorizon, s: facts.chunkS },
        },
        t,
      ),
    ],
    [
      "horizon",
      t("marvel0.hz.fact.horizon"),
      sayText(
        {
          key: "marvel0.hz.value.horizon",
          params: {
            n: facts.horizon,
            chunk: control.actionHorizon,
            s: facts.horizonS,
          },
        },
        t,
      ),
    ],
  ];
  if (facts.rtc) {
    const { reached, margin } = facts.rtc;
    const params = {
      h: facts.rtc.horizon,
      beats: facts.latencyBeats,
      reached,
      chunk: control.actionHorizon,
    };
    rows.push(
      [
        "beats",
        t("marvel0.hz.fact.beats"),
        sayText(
          {
            key: "marvel0.hz.value.beats",
            params: {
              beats: facts.latencyBeats,
              ms: facts.periodMs,
              s: control.latencyBeats / control.referenceHz,
            },
          },
          t,
        ),
      ],
      [
        "rtc",
        t("marvel0.hz.fact.rtc"),
        sayText(
          margin >= 0
            ? { key: "marvel0.hz.value.rtc_ok", params }
            : {
                key: "marvel0.hz.value.rtc_over",
                params: { ...params, over: -margin },
              },
          t,
        ),
      ],
    );
  }
  return (
    <>
      <Field
        className="rg-span"
        label={t("marvel0.hz.label")}
        hint={
          <>
            {t("marvel0.hz.hint")}
            {lowered !== null && facts.rtc && (
              <>
                {" "}
                {sayText(
                  {
                    key: "marvel0.hz.horizon_lowered",
                    params: { default: horizon, site: lowered },
                  },
                  t,
                )}
              </>
            )}
          </>
        }
        error={error}
      >
        <Select
          data-testid="marvel0-command-hz"
          value={value}
          disabled={disabled}
          onChange={(event) => onChange(event.target.value)}
        >
          {control.options.map((hz) => (
            <option key={hz} value={String(hz)}>
              {sayText(
                {
                  key:
                    hz === control.default
                      ? "marvel0.hz.option_default"
                      : "marvel0.hz.option",
                  params: { hz },
                },
                t,
              )}
            </option>
          ))}
        </Select>
      </Field>
      <div
        className="rg-span"
        data-testid="marvel0-command-rate-facts"
        data-hz={facts.hz}
        data-changed={facts.changed || undefined}
      >
        <h4>{t("marvel0.hz.facts.title")}</h4>
        <dl className="rg-facts">
          {rows.map(([name, label, text]) => (
            <div className="rg-fact" key={name} data-fact={name}>
              <dt>{label}</dt>
              <dd>{text}</dd>
            </div>
          ))}
        </dl>
        {facts.changed && (
          <p className="rg-hint">{t("marvel0.hz.facts.hint")}</p>
        )}
      </div>
      {facts.changed && (
        <div className="rg-span" data-testid="marvel0-command-rate-note">
          <Note tone="warning">
            {sayText({ key: "marvel0.hz.note", params: { hz: facts.hz } }, t)}
          </Note>
        </div>
      )}
    </>
  );
}

/** The model, RTC and guidance strength the command follows, read-only, with
 * the way back to where they are chosen. */
function Follow({
  selection,
  error,
  onUseAnother,
}: {
  selection: Effective;
  error?: string;
  onUseAnother: () => void;
}) {
  const { t } = useLocale();
  return (
    <div
      className="rg-follow rg-span"
      data-testid="marvel0-command-follow"
      data-source={selection.source}
    >
      <span className="rg-follow__label">{t("marvel0.cmd.field.model")}</span>
      <div className="rg-follow__row">
        <Badge tone="info">
          {sayText(
            {
              key: "marvel0.cmd.follow",
              params: { model: selection.modelId },
            },
            t,
          )}
        </Badge>
        <Button size="sm" variant="ghost" onClick={onUseAnother}>
          {t("marvel0.cmd.use_another")}
        </Button>
      </div>
      <p className="rg-muted">
        {wordOf(t, "marvel0.cmd.follow_", selection.source)}
      </p>
      <div
        className="rg-follow__row"
        data-testid="marvel0-command-follow-values"
      >
        <Badge tone="neutral">
          {sayText(
            {
              key: "marvel0.cmd.follow_rtc",
              params: {
                rtc: t(
                  selection.rtc
                    ? "marvel0.deploy.value.on"
                    : "marvel0.deploy.value.off",
                ),
              },
            },
            t,
          )}
        </Badge>
        {selection.cfgScale !== null && (
          <Badge tone="neutral">
            {sayText(
              {
                key: "marvel0.cmd.follow_cfg",
                params: { scale: selection.cfgScale },
              },
              t,
            )}
          </Badge>
        )}
      </div>
      {error && <p className="rg-bad">{error}</p>}
    </div>
  );
}

function CommandResult({
  preview,
  stale,
  modelLabel,
  start,
}: {
  preview: Preview;
  stale: boolean;
  modelLabel: string;
  /** The "Start the client" control, when the page hosts the client. */
  start?: ReactNode;
}) {
  const { t } = useLocale();
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const text = useRef<HTMLPreElement>(null);
  // Without the clipboard (a page that is not a secure context) the command is
  // selected, so that Ctrl+C copies it.
  const select = () => {
    try {
      if (text.current) window.getSelection()?.selectAllChildren(text.current);
    } catch {
      // nothing to select in
    }
  };
  const copy = () => {
    const write = navigator.clipboard?.writeText(preview.command);
    if (!write) return select();
    write
      .then(() => {
        setCopied(true);
        clearTimeout(timer.current);
        timer.current = setTimeout(() => setCopied(false), 2000);
      })
      .catch(select);
  };
  return (
    <section
      className="rg-result"
      data-testid="marvel0-command-result"
      data-stale={stale || undefined}
      aria-labelledby="marvel0-command-result-title"
    >
      <div className="rg-actions-row">
        <h4 id="marvel0-command-result-title">
          {t("marvel0.cmd.result.title")}
        </h4>
        {stale ? (
          <Badge tone="warning" icon={TriangleAlert}>
            {t("marvel0.cmd.result.stale")}
          </Badge>
        ) : (
          <Button
            size="sm"
            icon={copied ? Check : ClipboardCopy}
            onClick={copy}
          >
            {copied ? t("Copied") : t("Copy")}
          </Button>
        )}
      </div>
      <p className="rg-muted">{t("marvel0.cmd.result.hint")}</p>
      {!stale && start}
      {!stale && (
        <pre
          ref={text}
          lang="en"
          className="rg-command"
          data-testid="marvel0-command-text"
        >
          {preview.command}
        </pre>
      )}
      <p>
        {sayText(
          {
            key: "marvel0.cmd.result.needs",
            params: {
              model: modelLabel,
              rtc: t(
                preview.needs.rtc
                  ? "marvel0.deploy.value.on"
                  : "marvel0.deploy.value.off",
              ),
            },
          },
          t,
        )}
      </p>
      {preview.warnings.length > 0 && (
        <div data-testid="marvel0-command-warnings">
          <strong>{t("marvel0.cmd.warnings.title")}</strong>
          <ul className="rg-notes">
            {preview.warnings.map((warning) => (
              <li key={warning.code}>
                <WordedText worded={warningSay(warning)} />
              </li>
            ))}
          </ul>
        </div>
      )}
      <Note tone="warning">{t("marvel0.cmd.hands")}</Note>
    </section>
  );
}
