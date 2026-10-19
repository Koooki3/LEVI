"use client";
// The Policy server card of the Rig console: pick a model, RTC and a VRAM
// fraction, deploy the policy server in one confirmed press, see its state and
// log, and stop it. It starts and stops the one `systemd --user` unit this page
// created (LEVI's backend, levi/rig/deploy.py) and nothing else: not the arm
// stack, the gripper stack, the robot server or the demo client, and not a
// server started by hand. Nothing on it is an emergency stop.
//
// What the card shows is decided in deploy-logic.ts; what the server refuses is
// worded in rig-failure.ts. Both writes go through the same-origin proxy that
// adds the person's token (nothing here reads one), carry a request id kept
// across a retry, and need a typed phrase naming the action (and the model).
import { useEffect, useMemo, useRef, useState } from "react";
import {
  CircleCheck,
  CircleDashed,
  CircleHelp,
  CircleX,
  Clock,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  Dialog,
  Field,
  Input,
  Select,
  Switch,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { RequestIds } from "@/components/live/live-service-logic";
import { ReadNote, Refusal, WordedText } from "./action-parts";
import { ChecksBlock, LogBlock, ModelFacts, StatusBlock } from "./deploy-parts";
import {
  modelOptionText,
  problemSay,
  skippedSay,
  type ModelRow,
} from "./catalog-logic";
import type { ApiFailure, RigApi } from "./deploy-api";
import {
  STATE_KEY,
  STATE_TONE,
  deployBlock,
  deployBody,
  deployPhrase,
  deployResult,
  initialForm,
  newRequestId,
  noteSay,
  overridePhrase,
  parseScale,
  resolveForm,
  presetRows,
  stopBody,
  stopControl,
  stopPhrase,
  stopResult,
  type ActionResult,
  type DeployForm,
  type DeployState,
} from "./deploy-logic";
import { sayStopRefusal } from "./rig-failure";
import { sayText } from "./rig-say";
import { isFormat, type DeployPoll, type RigCatalog } from "./use-rig-actions";

const STATE_ICON: Record<DeployState, LucideIcon> = {
  idle: CircleDashed,
  starting: Clock,
  ready: CircleCheck,
  failed: CircleX,
  dead: CircleX,
  unknown: CircleHelp,
  foreign_listener: TriangleAlert,
  stopping: Clock,
};
const EDGE: Record<DeployState, string> = {
  idle: "unknown",
  starting: "stale",
  ready: "ok",
  failed: "red",
  dead: "red",
  unknown: "unknown",
  foreign_listener: "stale",
  stopping: "stale",
};
type Pending = "deploy" | "stop" | null;
interface Problem {
  action: "deploy" | "stop";
  failure: ApiFailure;
}

export function DeployPanel({
  api,
  catalog,
  poll,
  onLogOpen,
}: {
  api: RigApi;
  catalog: RigCatalog;
  poll: DeployPoll;
  onLogOpen: (open: boolean) => void;
}) {
  const { t } = useLocale();
  const status = poll.status;
  const site = catalog.site;
  const models = catalog.models?.models ?? [];
  const initial = useMemo(
    () => (catalog.models && site ? initialForm(catalog.models, site) : null),
    [catalog.models, site],
  );
  const [edits, setEdits] = useState<Partial<DeployForm>>({});
  const [dialog, setDialog] = useState<"deploy" | "stop" | null>(null);
  const [phrase, setPhrase] = useState("");
  const [override, setOverride] = useState("");
  const [pending, setPending] = useState<Pending>(null);
  const [result, setResult] = useState<ActionResult | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const busy = useRef(false);
  const ids = useRef(new RequestIds(newRequestId)).current;

  const form: DeployForm | null =
    initial && catalog.models && site
      ? resolveForm(initial, edits, catalog.models, site)
      : null;
  const modelId = form?.modelId ?? "";
  const model: ModelRow | null = models.find((m) => m.id === modelId) ?? null;
  const edit = (change: Partial<DeployForm>) =>
    setEdits((current) => ({ ...current, ...change }));

  const control = stopControl(status);
  const current = poll.current && status !== null;
  const block =
    form && current
      ? deployBlock(status, form, model, site, pending !== null)
      : null;
  const deployOff = !form || !model || !current || block !== null;
  const stopOff = !current || pending !== null || control.operation;
  const state: DeployState = status ? status.state : "unknown";
  const rows = status ? presetRows(status) : [];
  const scale = form && model ? parseScale(form.scale, model, site) : null;

  const close = () => {
    setDialog(null);
    setPhrase("");
    setOverride("");
    // The acceptance of an unverified model is for this deploy only.
    setEdits((now) =>
      now.acceptUnverified ? { ...now, acceptUnverified: false } : now,
    );
  };
  // A dialog confirms the state it was opened on. When that state is lost (a
  // read failed, or the answer is too old to show), the dialog and what was
  // typed in it go too: a state read later needs a fresh confirmation.
  useEffect(() => {
    if (!current) {
      setDialog(null);
      setPhrase("");
      setOverride("");
      setEdits((now) =>
        now.acceptUnverified ? { ...now, acceptUnverified: false } : now,
      );
    }
  }, [current]);

  async function send(action: "deploy" | "stop") {
    if (busy.current || !form || !status) return;
    busy.current = true;
    setPending(action);
    setProblem(null);
    setResult(null);
    const id = ids.next(action);
    let answer;
    try {
      answer =
        action === "deploy"
          ? await api.startDeploy(deployBody(id, form, model!, phrase, site))
          : await api.stopDeploy(
              stopBody(id, phrase, override, overridePhrase(status)),
            );
    } finally {
      busy.current = false;
      setPending(null);
    }
    if (answer.ok) {
      ids.release();
      const read =
        action === "deploy"
          ? deployResult(answer.data)
          : stopResult(answer.data);
      // The write went through; only its answer is not understood, so the
      // state read below is what to go by (not "Deploy and Stop are off").
      setResult(
        read ?? {
          tone: "warning",
          lines: [{ key: "rig.deploy.accepted_unread" }],
        },
      );
      close();
    } else {
      // A refusal is final for its request id. With no answer at all it is not
      // known whether the request arrived: the id is kept, so a press again
      // repeats that request, which is never carried out twice.
      if (answer.status !== 0) ids.release();
      setProblem({ action, failure: answer });
      // A stop that met a client stays open: it needs the extra phrase.
      if (!(action === "stop" && answer.code === "client_connected")) close();
    }
    poll.refresh();
  }

  const clientsNow =
    (control.clients ?? 0) > 0 || problem?.failure.code === "client_connected";
  // The dialogs confirm only what the card would still offer: a state that
  // went stale, or changed under an open dialog, switches the button off.
  const deployReady =
    !deployOff &&
    !!model &&
    !!status &&
    phrase === deployPhrase(status, modelId) &&
    (model.verified || !!form?.acceptUnverified);
  const stopReady =
    current &&
    control.show &&
    !control.operation &&
    !!status &&
    phrase === stopPhrase(status) &&
    (!clientsNow || override === overridePhrase(status));

  const edge = EDGE[state];
  return (
    <>
      <Card
        padding="compact"
        className={`rg-card rg-action rg-card--${edge}`}
        data-testid="rig-deploy"
        data-state={poll.loaded ? state : undefined}
        title={t("rig.deploy.title")}
        description={t("rig.deploy.scope")}
        actions={
          status ? (
            <Badge tone={STATE_TONE[state]} icon={STATE_ICON[state]}>
              {t(STATE_KEY[state])}
            </Badge>
          ) : null
        }
      >
        {status?.estop.keepHand && (
          <div data-testid="rig-deploy-keep-hand">
            <Note tone="warning" role="alert">
              {sayText(
                {
                  key: "rig.deploy.keep_hand",
                  params: { state: { key: STATE_KEY[state] } },
                },
                t,
              )}
            </Note>
          </div>
        )}
        {!poll.loaded && (
          <p role="status" className="rg-muted">
            {t("rig.deploy.reading")}
          </p>
        )}
        {poll.loaded && !poll.current && (
          <div className="rg-unreadable">
            {!(poll.problem && isFormat(poll.problem)) && (
              <Note tone="warning" role="status">
                {t("rig.deploy.unreadable")}
              </Note>
            )}
            {poll.problem && (
              <ReadNote problem={poll.problem} formatKey="rig.deploy.format" />
            )}
          </div>
        )}
        {status && <StatusBlock status={status} current={poll.current} />}
        {result && (
          <div data-testid="rig-deploy-result" className="rg-result">
            <Note tone={result.tone} role="status">
              {result.lines.map((line, index) => (
                <p key={index}>{sayText(line, t)}</p>
              ))}
              <Button size="sm" variant="ghost" onClick={() => setResult(null)}>
                {t("Close")}
              </Button>
            </Note>
          </div>
        )}
        {problem && !(dialog === "stop" && problem.action === "stop") && (
          <Refusal
            testId="rig-deploy-problem"
            title={
              problem.action === "deploy"
                ? "rig.deploy.refused.deploy"
                : "rig.deploy.refused.stop"
            }
            failure={problem.failure}
            context={{ write: true, site }}
            worded={
              problem.action === "stop"
                ? sayStopRefusal(problem.failure, { write: true, site })
                : undefined
            }
          />
        )}

        {form && catalog.models && site ? (
          <div className="rg-form">
            {models.length === 0 ? (
              <p className="rg-bad">{t("rig.deploy.form.no_models")}</p>
            ) : (
              <>
                <Field label={t("rig.deploy.form.model")}>
                  <Select
                    value={modelId}
                    onChange={(event) =>
                      edit({
                        modelId: event.target.value,
                        scale: "",
                        acceptUnverified: false,
                      })
                    }
                  >
                    {models.map((entry) => (
                      <option key={entry.id} value={entry.id}>
                        {modelOptionText(entry, t)}
                      </option>
                    ))}
                  </Select>
                </Field>
                {model && <ModelFacts model={model} />}
                <Switch
                  label={t("rig.deploy.form.rtc")}
                  description={t("rig.deploy.form.rtc_hint")}
                  checked={form.rtc}
                  onChange={(event) => edit({ rtc: event.target.checked })}
                />
                <Field label={t("rig.deploy.form.fraction")}>
                  <Select
                    value={form.fraction === null ? "" : String(form.fraction)}
                    onChange={(event) =>
                      edit({ fraction: Number(event.target.value) })
                    }
                  >
                    {site.gpu.presets.map((fraction) => (
                      <option key={fraction} value={String(fraction)}>
                        {fraction.toFixed(2)}
                      </option>
                    ))}
                  </Select>
                </Field>
                {rows.length > 0 && (
                  <div className="rg-presets">
                    <strong>{t("rig.deploy.presets.title")}</strong>
                    <ul className="rg-notes" data-testid="rig-deploy-presets">
                      {rows.map((row) => (
                        <li
                          key={row.fraction}
                          aria-current={
                            row.fraction === form.fraction ? "true" : undefined
                          }
                        >
                          <strong>{row.label}</strong>
                          {": "}
                          {sayText(row.say, t)}
                          {row.enough === false && row.liveNote && (
                            <> {t("rig.deploy.preset.live_note")}</>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {model?.cfgCapable && (
                  <Field
                    label={t("rig.deploy.form.scale")}
                    hint={sayText(
                      {
                        key: "rig.deploy.form.scale_hint",
                        params: {
                          default: site.caps.cfgScale.default,
                          max: site.caps.cfgScale.max,
                        },
                      },
                      t,
                    )}
                    error={
                      scale?.error
                        ? sayText(
                            {
                              key: "rig.deploy.block.scale",
                              params: { max: site.caps.cfgScale.max },
                            },
                            t,
                          )
                        : undefined
                    }
                  >
                    <Input
                      value={form.scale}
                      inputMode="decimal"
                      autoComplete="off"
                      spellCheck={false}
                      placeholder={String(site.caps.cfgScale.default)}
                      onChange={(event) => edit({ scale: event.target.value })}
                    />
                  </Field>
                )}
              </>
            )}
            <div className="rg-actions-row">
              <Button
                variant="primary"
                loading={pending === "deploy"}
                disabled={deployOff}
                onClick={() => setDialog("deploy")}
              >
                {t("rig.deploy.btn.deploy")}
              </Button>
              {control.show && (
                <Button
                  variant="danger"
                  loading={pending === "stop"}
                  disabled={stopOff}
                  onClick={() => setDialog("stop")}
                >
                  {t("rig.deploy.btn.stop")}
                </Button>
              )}
              {block && (
                <span className="rg-muted" role="status">
                  <WordedText worded={block} />
                </span>
              )}
            </div>
          </div>
        ) : (
          <div className="rg-actions-row">
            <Button variant="primary" disabled>
              {t("rig.deploy.btn.deploy")}
            </Button>
          </div>
        )}

        {catalog.models && catalog.models.skipped.length > 0 && (
          <details className="rg-details">
            <summary>
              {sayText(
                {
                  key: "rig.deploy.skipped.title",
                  params: { n: catalog.models.skipped.length },
                },
                t,
              )}
            </summary>
            <ul className="rg-notes">
              {catalog.models.skipped.map((entry) => (
                <li key={entry.id}>
                  <code lang="en">{entry.id}</code>
                  {": "}
                  <WordedText worded={skippedSay(entry.reason)} />
                </li>
              ))}
            </ul>
          </details>
        )}
        {catalog.models && catalog.models.problems.length > 0 && (
          <ul className="rg-notes">
            {catalog.models.problems.map((entry) => (
              <li key={entry}>
                <WordedText worded={problemSay(entry)} />
              </li>
            ))}
          </ul>
        )}

        {status && <ChecksBlock status={status} />}
        {status && status.notes.length > 0 && (
          <ul className="rg-notes">
            {status.notes.map((note) => (
              <li key={note.code}>
                <WordedText worded={noteSay(note)} />
              </li>
            ))}
          </ul>
        )}
        {status?.deploy && (
          <LogBlock
            poll={poll}
            open={state === "failed" || state === "dead"}
            onOpen={onLogOpen}
          />
        )}
      </Card>

      {form && model && status && (
        <Dialog
          open={dialog === "deploy"}
          onClose={() => {
            if (pending === null) close();
          }}
          title={sayText(
            {
              key: "rig.deploy.dialog.deploy_title",
              params: { model: model.id },
            },
            t,
          )}
          description={t("rig.deploy.dialog.deploy_intro")}
          footer={
            <>
              <Button
                variant="secondary"
                disabled={pending !== null}
                onClick={close}
              >
                {t("rig.deploy.btn.cancel")}
              </Button>
              <Button
                variant="primary"
                loading={pending === "deploy"}
                disabled={!deployReady}
                onClick={() => void send("deploy")}
              >
                {t("rig.deploy.btn.deploy")}
              </Button>
            </>
          }
        >
          <dl className="rg-facts">
            {[
              [t("rig.deploy.fact.model"), <code key="m">{model.id}</code>],
              [
                t("rig.deploy.fact.config"),
                <code key="c">{model.config}</code>,
              ],
              [
                t("rig.deploy.fact.rtc"),
                t(
                  form.rtc
                    ? "rig.deploy.dialog.rtc_on"
                    : "rig.deploy.dialog.rtc_off",
                ),
              ],
              [
                t("rig.deploy.fact.fraction"),
                form.fraction === null ? "?" : form.fraction.toFixed(2),
              ],
              [
                t("rig.deploy.fact.gpu"),
                (rows.find((row) => row.fraction === form.fraction)?.say &&
                  sayText(
                    rows.find((row) => row.fraction === form.fraction)!.say,
                    t,
                  )) ??
                  "?",
              ],
            ].map(([label, value], index) => (
              <div key={index} className="rg-fact">
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          <Note tone="warning">{t("rig.deploy.dialog.hand")}</Note>
          {!model.verified && (
            <Checkbox
              checked={form.acceptUnverified}
              onChange={(event) =>
                edit({ acceptUnverified: event.target.checked })
              }
              label={t("rig.deploy.model.accept")}
              description={t("rig.deploy.model.unverified_hint")}
            />
          )}
          <Field
            label={t("rig.deploy.dialog.phrase")}
            hint={sayText(
              {
                key: "rig.deploy.dialog.phrase_hint",
                params: { phrase: deployPhrase(status, model.id) },
              },
              t,
            )}
          >
            <Input
              value={phrase}
              onChange={(event) => setPhrase(event.target.value)}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </Field>
        </Dialog>
      )}

      {status && (
        <Dialog
          open={dialog === "stop"}
          onClose={() => {
            if (pending === null) close();
          }}
          title={t("rig.deploy.dialog.stop_title")}
          description={sayText(
            {
              key: "rig.deploy.dialog.stop_intro",
              params: { unit: status.deploy?.unit ?? "?" },
            },
            t,
          )}
          footer={
            <>
              <Button
                variant="secondary"
                disabled={pending !== null}
                onClick={close}
              >
                {t("rig.deploy.btn.cancel")}
              </Button>
              <Button
                variant="danger"
                loading={pending === "stop"}
                disabled={!stopReady}
                onClick={() => void send("stop")}
              >
                {t("rig.deploy.btn.stop")}
              </Button>
            </>
          }
        >
          {clientsNow && (
            <Note tone="warning" role="alert">
              {t("rig.deploy.dialog.stop_client")}
            </Note>
          )}
          {problem?.action === "stop" && dialog === "stop" && (
            <p className="rg-bad" role="status">
              <WordedText
                worded={sayStopRefusal(problem.failure, { write: true, site })}
              />
            </p>
          )}
          <Field
            label={t("rig.deploy.dialog.phrase")}
            hint={sayText(
              {
                key: "rig.deploy.dialog.phrase_hint",
                params: { phrase: stopPhrase(status) },
              },
              t,
            )}
          >
            <Input
              value={phrase}
              onChange={(event) => setPhrase(event.target.value)}
              autoComplete="off"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </Field>
          {clientsNow && (
            <Field
              label={t("rig.deploy.dialog.override")}
              hint={sayText(
                {
                  key: "rig.deploy.dialog.phrase_hint",
                  params: { phrase: overridePhrase(status) },
                },
                t,
              )}
            >
              <Input
                value={override}
                onChange={(event) => setOverride(event.target.value)}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
              />
            </Field>
          )}
          {site && (
            <p className="rg-muted">
              {sayText(
                {
                  key: "rig.deploy.dialog.stop_wait",
                  params: { s: (site.unit.stopTimeoutS ?? 0) + 30 },
                },
                t,
              )}
            </p>
          )}
        </Dialog>
      )}
    </>
  );
}
