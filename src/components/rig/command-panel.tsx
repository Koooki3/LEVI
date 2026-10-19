"use client";
// The Client command card of the Rig console: a form for the demo client's
// run (task instruction, steps, eval number, lowest height, RTC horizon, group,
// note) and the exact command for it, to copy. LEVI's backend validates the
// form and renders the command (levi/rig/command.py); every flag that guards
// the arm comes from the site file and is listed here, never editable. Nothing
// is executed or started by this card: a person pastes the command into a
// terminal, with a hand on the physical e-stop.
import { useMemo, useRef, useState, useEffect } from "react";
import { Check, ClipboardCopy, TriangleAlert } from "lucide-react";
import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Select,
  Switch,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { Refusal, WordedText } from "./action-parts";
import { modelOptionText } from "./catalog-logic";
import {
  bodyKey,
  commandBody,
  initialCommandForm,
  placeholders,
  readPreview,
  serverMatch,
  warningSay,
  type CommandForm,
  type FieldName,
  type Preview,
} from "./command-logic";
import type { ApiFailure, RigApi } from "./deploy-api";
import type { DeployStatus } from "./deploy-logic";
import { fieldMessages } from "./rig-failure";
import type { Say } from "./rig-logic";
import { sayText } from "./rig-say";
import type { RigCatalog } from "./use-rig-actions";

interface Made {
  preview: Preview;
  /** The request it was made for: a form that differs has outgrown it. */
  key: string;
}

export function CommandPanel({
  api,
  catalog,
  status,
  deployedModel,
}: {
  api: RigApi;
  catalog: RigCatalog;
  /** The policy server's state, to say whether it fits the command. */
  status: DeployStatus | null;
  /** The model of the server this page deployed, to start the form on it. */
  deployedModel: string | null;
}) {
  const { t } = useLocale();
  const models = catalog.models;
  const site = catalog.site;
  const initial = useMemo(
    () =>
      models && site
        ? initialCommandForm(models, site, deployedModel ?? "")
        : null,
    [models, site, deployedModel],
  );
  const [edits, setEdits] = useState<Partial<CommandForm>>({});
  const [made, setMade] = useState<Made | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [local, setLocal] = useState<Partial<Record<FieldName, Say>>>({});
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  if (!models || !site || !initial) return null;

  const form: CommandForm = { ...initial, ...edits };
  if (!models.models.some((m) => m.id === form.modelId))
    form.modelId = initial.modelId;
  const edit = (change: Partial<CommandForm>) =>
    setEdits((cur) => ({ ...cur, ...change }));
  const model = models.models.find((m) => m.id === form.modelId) ?? null;
  const hints = placeholders(form, models, site);
  const { body, errors } = commandBody(form);
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
      data-testid="rig-command"
      title={t("rig.cmd.title")}
      description={t("rig.cmd.scope")}
    >
      <form
        className="rg-form"
        onSubmit={(event) => {
          event.preventDefault();
          void make();
        }}
      >
        {models.models.length === 0 ? (
          <p className="rg-bad">{t("rig.deploy.form.no_models")}</p>
        ) : (
          <>
            <Field
              label={t("rig.cmd.field.model")}
              error={fieldError("model_id")}
            >
              <Select
                value={form.modelId}
                onChange={(event) => edit({ modelId: event.target.value })}
              >
                {models.models.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {modelOptionText(entry, t)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label={t("rig.cmd.field.prompt")}
              hint={
                <>
                  {sayText(
                    {
                      key: "rig.cmd.field.prompt_hint",
                      params: { n: site.caps.promptMaxChars },
                    },
                    t,
                  )}
                  {model?.restrictedPrompts && (
                    <> {t("rig.deploy.model.restricted")}.</>
                  )}
                </>
              }
              error={fieldError("prompt")}
            >
              <Input
                value={form.prompt}
                list="rig-known-prompts"
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                lang="en"
                onChange={(event) => edit({ prompt: event.target.value })}
              />
            </Field>
            <datalist id="rig-known-prompts">
              {(model?.restrictedPrompts
                ? site.prompts.restricted
                : site.prompts.known
              ).map((prompt) => (
                <option key={prompt} value={prompt} />
              ))}
            </datalist>
            <Field
              label={t("rig.cmd.field.max_steps")}
              hint={sayText(
                {
                  key: "rig.cmd.field.max_steps_hint",
                  params: { cap: site.caps.maxSteps.cap },
                },
                t,
              )}
              error={fieldError("max_steps")}
            >
              <Input
                value={form.maxSteps}
                inputMode="numeric"
                autoComplete="off"
                onChange={(event) => edit({ maxSteps: event.target.value })}
              />
            </Field>
            <Field
              label={t("rig.cmd.field.eval_num")}
              hint={sayText(
                {
                  key: "rig.cmd.field.eval_num_hint",
                  params: { cap: site.caps.evalNum.cap },
                },
                t,
              )}
              error={fieldError("eval_num")}
            >
              <Input
                value={form.evalNum}
                inputMode="numeric"
                autoComplete="off"
                onChange={(event) => edit({ evalNum: event.target.value })}
              />
            </Field>
            <Field
              label={t("rig.cmd.field.min_z")}
              hint={sayText(
                {
                  key: "rig.cmd.field.min_z_hint",
                  params: { floor: site.minZ.floor, home: site.homeZ },
                },
                t,
              )}
              error={fieldError("min_z")}
            >
              <Input
                value={form.minZ}
                inputMode="decimal"
                autoComplete="off"
                onChange={(event) => edit({ minZ: event.target.value })}
              />
            </Field>
            <Switch
              label={t("rig.cmd.field.rtc")}
              description={t("rig.cmd.field.rtc_hint")}
              checked={form.rtc}
              onChange={(event) => edit({ rtc: event.target.checked })}
            />
            <Field
              label={t(
                form.rtc
                  ? "rig.cmd.field.horizon_rtc"
                  : "rig.cmd.field.horizon_open",
              )}
              hint={sayText(
                {
                  key: "rig.cmd.field.horizon_hint",
                  params: { max: hints.horizonMax, default: hints.horizon },
                },
                t,
              )}
              error={fieldError("rtc_horizon")}
            >
              <Input
                value={form.horizon}
                inputMode="numeric"
                autoComplete="off"
                placeholder={hints.horizon}
                onChange={(event) => edit({ horizon: event.target.value })}
              />
            </Field>
            <Field
              label={t("rig.cmd.field.group")}
              hint={t("rig.cmd.field.group_hint")}
              error={fieldError("rollout_group")}
            >
              <Input
                value={form.group}
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
              label={t("rig.cmd.field.note")}
              hint={sayText(
                {
                  key: "rig.cmd.field.note_hint",
                  params: { n: site.caps.noteMaxChars },
                },
                t,
              )}
              error={fieldError("note")}
            >
              <Input
                value={form.note}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                onChange={(event) => edit({ note: event.target.value })}
              />
            </Field>
          </>
        )}
        <details className="rg-details">
          <summary>{t("rig.cmd.fixed.title")}</summary>
          <p className="rg-muted">{t("rig.cmd.fixed.hint")}</p>
          <ul className="rg-notes" data-testid="rig-command-fixed" lang="en">
            {site.fixedFlags.map((flag) => (
              <li key={flag}>
                <code>{flag}</code>
              </li>
            ))}
          </ul>
        </details>
        <div className="rg-actions-row">
          <Button
            type="submit"
            variant="primary"
            loading={pending}
            disabled={!model}
          >
            {t("rig.cmd.btn.make")}
          </Button>
        </div>
      </form>

      <p
        className={server.good ? "rg-muted" : "rg-warn"}
        data-testid="rig-command-server"
        data-kind={server.kind}
      >
        {sayText(server.say, t)}
      </p>

      {otherFailure && (
        <Refusal
          testId="rig-command-problem"
          title="rig.cmd.result.failed"
          failure={otherFailure}
          context={{ site }}
          worded={
            otherFailure.code === "format"
              ? { say: { key: "rig.act.models_format" }, text: "" }
              : undefined
          }
        />
      )}

      {made && (
        <CommandResult
          preview={made.preview}
          stale={stale}
          modelLabel={made.preview.needs.modelId}
        />
      )}
    </Card>
  );
}

function CommandResult({
  preview,
  stale,
  modelLabel,
}: {
  preview: Preview;
  stale: boolean;
  modelLabel: string;
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
      data-testid="rig-command-result"
      data-stale={stale || undefined}
      aria-labelledby="rig-command-result-title"
    >
      <div className="rg-actions-row">
        <h4 id="rig-command-result-title">{t("rig.cmd.result.title")}</h4>
        {stale ? (
          <Badge tone="warning" icon={TriangleAlert}>
            {t("rig.cmd.result.stale")}
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
      <p className="rg-muted">{t("rig.cmd.result.hint")}</p>
      <pre
        ref={text}
        lang="en"
        className={`rg-command${stale ? " rg-command--stale" : ""}`}
        data-testid="rig-command-text"
      >
        {preview.command}
      </pre>
      <p>
        {sayText(
          {
            key: "rig.cmd.result.needs",
            params: {
              model: modelLabel,
              rtc: t(
                preview.needs.rtc
                  ? "rig.deploy.value.on"
                  : "rig.deploy.value.off",
              ),
            },
          },
          t,
        )}
      </p>
      {preview.warnings.length > 0 && (
        <div data-testid="rig-command-warnings">
          <strong>{t("rig.cmd.warnings.title")}</strong>
          <ul className="rg-notes">
            {preview.warnings.map((warning) => (
              <li key={warning.code}>
                <WordedText worded={warningSay(warning)} />
              </li>
            ))}
          </ul>
        </div>
      )}
      <Note tone="warning">{t("rig.cmd.hands")}</Note>
    </section>
  );
}
