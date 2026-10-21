"use client";
// The Policy server card of the marvel0: pick a model, RTC and a VRAM
// fraction, deploy the policy server in one confirmed press, see its state and
// log, and stop it. It starts and stops the one `systemd --user` unit this page
// created (LEVI's backend, levi/marvel0/deploy.py) and nothing else: not the arm
// stack, the gripper stack, the robot server or the demo client, and not a
// server started by hand. Nothing on it is an emergency stop.
//
// What the card shows is decided in deploy-logic.ts; what the server refuses is
// worded in marvel0-failure.ts. Both writes go through the same-origin proxy that
// adds the person's token (nothing here reads one), carry a request id kept
// across a retry, and are confirmed in a window (title, what the action does,
// Cancel with the initial focus, Confirm). The page sends the action's fixed
// confirmation phrase itself; the one phrase a person types is the extra one
// that stops the server under a connected client.
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
  Field,
  Input,
  Select,
  Switch,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note } from "@/components/pages-ui/feedback";
import { RequestIds } from "@/components/live/live-service-logic";
import { ReadNote, Refusal, WordedText } from "./action-parts";
import { ConfirmRefusal, ConfirmWindow } from "./confirm-window";
import { ChecksBlock, LogBlock, ModelFacts, StatusBlock } from "./deploy-parts";
import {
  modelOptionText,
  problemSay,
  skippedSay,
  type ModelRow,
} from "./catalog-logic";
import type { ApiFailure, Marvel0Api } from "./deploy-api";
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
import { sayStopRefusal } from "./marvel0-failure";
import { sayText } from "./marvel0-say";
import {
  isFormat,
  type DeployPoll,
  type Marvel0Catalog,
} from "./use-marvel0-actions";
import {
  effectiveSelection,
  serverHolding,
  type ChoiceState,
} from "./selection";

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

/** Marks the model control: the command card's "use another model" link moves
 * the keyboard to the element that carries it. (Not an id: the label of the
 * field points at the control by the id the field gives it.) */
export const MODEL_CONTROL = "data-model-control";

export function DeployPanel({
  api,
  catalog,
  poll,
  onLogOpen,
  choice: shared,
}: {
  api: Marvel0Api;
  catalog: Marvel0Catalog;
  poll: DeployPoll;
  onLogOpen: (open: boolean) => void;
  /** The page's one selected model (selection.ts): what is chosen here is
   * what the command and the run follow. */
  choice: ChoiceState;
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
  // The model the deploy window was opened for. The window confirms that
  // model and no other: when the selection moves under it, it closes.
  const [pinned, setPinned] = useState<string | null>(null);
  const [override, setOverride] = useState("");
  const [pending, setPending] = useState<Pending>(null);
  const [result, setResult] = useState<ActionResult | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const busy = useRef(false);
  const ids = useRef(new RequestIds(newRequestId)).current;

  // The model, RTC and guidance strength are the page's one selection: this
  // card chooses them, the command and the run follow. While the policy
  // server of this page runs, what it runs is the selection and the controls
  // are read-only.
  const effective = effectiveSelection(catalog.models, shared.choice, status);
  const held = serverHolding(status) !== null && effective?.source === "server";
  const form: DeployForm | null =
    initial && catalog.models && site
      ? resolveForm(
          initial,
          {
            ...edits,
            ...(effective
              ? { modelId: effective.modelId, rtc: effective.rtc }
              : {}),
            scale: held
              ? effective?.cfgScale === null ||
                effective?.cfgScale === undefined
                ? ""
                : String(effective.cfgScale)
              : shared.choice.scale,
          },
          catalog.models,
          site,
        )
      : null;
  const modelId = form?.modelId ?? "";
  const model: ModelRow | null = models.find((m) => m.id === modelId) ?? null;
  const edit = (change: Partial<DeployForm>) => {
    const { modelId: pickedModel, rtc, scale: typedScale, ...own } = change;
    if (
      pickedModel !== undefined ||
      rtc !== undefined ||
      typedScale !== undefined
    )
      shared.pick({
        ...(pickedModel !== undefined ? { modelId: pickedModel } : {}),
        ...(rtc !== undefined ? { rtc } : {}),
        ...(typedScale !== undefined ? { scale: typedScale } : {}),
      });
    if (Object.keys(own).length > 0)
      setEdits((current) => ({ ...current, ...own }));
  };

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
    setPinned(null);
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
      setPinned(null);
      setOverride("");
      setEdits((now) =>
        now.acceptUnverified ? { ...now, acceptUnverified: false } : now,
      );
    }
  }, [current]);
  // The deploy window is for the model it was opened for: another model
  // (picked by the card, the command card or a server that now holds the
  // selection) closes it, and what was ticked in it goes with it.
  const movedOff = dialog === "deploy" && pinned !== modelId;
  useEffect(() => {
    if (movedOff) {
      setDialog(null);
      setPinned(null);
      setEdits((now) =>
        now.acceptUnverified ? { ...now, acceptUnverified: false } : now,
      );
    }
  }, [movedOff]);

  async function send(action: "deploy" | "stop") {
    if (busy.current || !form || !status) return;
    // Never another model than the one the window was opened for.
    if (action === "deploy" && pinned !== modelId) return;
    busy.current = true;
    setPending(action);
    setProblem(null);
    setResult(null);
    const id = ids.next(action);
    let answer;
    try {
      answer =
        action === "deploy"
          ? await api.startDeploy(
              deployBody(id, form, model!, deployPhrase(status, modelId), site),
            )
          : await api.stopDeploy(
              stopBody(
                id,
                stopPhrase(status),
                override,
                overridePhrase(status),
              ),
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
          lines: [{ key: "marvel0.deploy.accepted_unread" }],
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
    pinned === modelId &&
    !!model &&
    !!status &&
    (model.verified || !!form?.acceptUnverified);
  const stopReady =
    current &&
    control.show &&
    !control.operation &&
    !!status &&
    (!clientsNow || override === overridePhrase(status));

  const edge = EDGE[state];
  return (
    <>
      <Card
        padding="compact"
        className={`rg-card rg-action rg-card--${edge}`}
        data-testid="marvel0-deploy"
        data-state={poll.loaded ? state : undefined}
        title={t("marvel0.deploy.title")}
        description={t("marvel0.deploy.scope")}
        actions={
          status ? (
            <Badge tone={STATE_TONE[state]} icon={STATE_ICON[state]}>
              {t(STATE_KEY[state])}
            </Badge>
          ) : null
        }
      >
        {status?.estop.keepHand && (
          <div data-testid="marvel0-deploy-keep-hand">
            <Note tone="warning" role="alert">
              {sayText(
                {
                  key: "marvel0.deploy.keep_hand",
                  params: { state: { key: STATE_KEY[state] } },
                },
                t,
              )}
            </Note>
          </div>
        )}
        {!poll.loaded && (
          <p role="status" className="rg-muted">
            {t("marvel0.deploy.reading")}
          </p>
        )}
        {poll.loaded && !poll.current && (
          <div className="rg-unreadable">
            {!(poll.problem && isFormat(poll.problem)) && (
              <Note tone="warning" role="status">
                {t("marvel0.deploy.unreadable")}
              </Note>
            )}
            {poll.problem && (
              <ReadNote
                problem={poll.problem}
                formatKey="marvel0.deploy.format"
              />
            )}
          </div>
        )}
        {status && <StatusBlock status={status} current={poll.current} />}
        {result && (
          <div data-testid="marvel0-deploy-result" className="rg-result">
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
            testId="marvel0-deploy-problem"
            title={
              problem.action === "deploy"
                ? "marvel0.deploy.refused.deploy"
                : "marvel0.deploy.refused.stop"
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
              <p className="rg-bad">{t("marvel0.deploy.form.no_models")}</p>
            ) : (
              <>
                <Field label={t("marvel0.deploy.form.model")}>
                  <Select
                    {...{ [MODEL_CONTROL]: "true" }}
                    value={modelId}
                    disabled={held}
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
                {held && (
                  <p className="rg-muted" data-testid="marvel0-deploy-held">
                    {t("marvel0.deploy.form.held")}
                  </p>
                )}
                <Switch
                  label={t("marvel0.deploy.form.rtc")}
                  description={t("marvel0.deploy.form.rtc_hint")}
                  checked={form.rtc}
                  disabled={held}
                  onChange={(event) => edit({ rtc: event.target.checked })}
                />
                <Field label={t("marvel0.deploy.form.fraction")}>
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
                  // What each VRAM fraction needs: folded (the Deploy button
                  // says when memory is short), open when the chosen fraction
                  // does not fit.
                  <details
                    className="rg-details rg-presets"
                    open={
                      rows.find((row) => row.fraction === form.fraction)
                        ?.enough === false
                        ? true
                        : undefined
                    }
                  >
                    <summary>{t("marvel0.deploy.presets.title")}</summary>
                    <ul
                      className="rg-notes"
                      data-testid="marvel0-deploy-presets"
                    >
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
                            <> {t("marvel0.deploy.preset.live_note")}</>
                          )}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                {model?.cfgCapable && (
                  <Field
                    label={t("marvel0.deploy.form.scale")}
                    hint={sayText(
                      {
                        key: "marvel0.deploy.form.scale_hint",
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
                              key: "marvel0.deploy.block.scale",
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
                      disabled={held}
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
                onClick={() => {
                  setPinned(modelId);
                  setDialog("deploy");
                }}
              >
                {t("marvel0.deploy.btn.deploy")}
              </Button>
              {control.show && (
                // Ends a process the client depends on: outlined in the
                // danger colour; only HOLD is a solid danger button.
                <Button
                  variant="secondary"
                  className="rg-btn--danger-outline"
                  loading={pending === "stop"}
                  disabled={stopOff}
                  onClick={() => setDialog("stop")}
                >
                  {t("marvel0.deploy.btn.stop")}
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
              {t("marvel0.deploy.btn.deploy")}
            </Button>
          </div>
        )}

        {catalog.models && catalog.models.skipped.length > 0 && (
          <details className="rg-details">
            <summary>
              {sayText(
                {
                  key: "marvel0.deploy.skipped.title",
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
        <ConfirmWindow
          open={dialog === "deploy" && !movedOff}
          testId="marvel0-deploy-dialog"
          title={sayText(
            {
              key: "marvel0.deploy.dialog.deploy_title",
              params: { model: model.id },
            },
            t,
          )}
          intro={t("marvel0.deploy.dialog.deploy_intro")}
          estop={t("marvel0.deploy.dialog.hand")}
          facts={
            <dl className="rg-facts">
              {[
                [
                  t("marvel0.deploy.fact.model"),
                  <code key="m">{model.id}</code>,
                ],
                [
                  t("marvel0.deploy.fact.config"),
                  <code key="c">{model.config}</code>,
                ],
                [
                  t("marvel0.deploy.fact.rtc"),
                  t(
                    form.rtc
                      ? "marvel0.deploy.dialog.rtc_on"
                      : "marvel0.deploy.dialog.rtc_off",
                  ),
                ],
                [
                  t("marvel0.deploy.fact.fraction"),
                  form.fraction === null ? "?" : form.fraction.toFixed(2),
                ],
                [
                  t("marvel0.deploy.fact.gpu"),
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
          }
          after={
            !model.verified ? (
              <Checkbox
                checked={form.acceptUnverified}
                onChange={(event) =>
                  edit({ acceptUnverified: event.target.checked })
                }
                label={t("marvel0.deploy.model.accept")}
                description={t("marvel0.deploy.model.unverified_hint")}
              />
            ) : null
          }
          ready={deployReady}
          confirmLabel={t("marvel0.deploy.btn.deploy")}
          working={pending !== null}
          onCancel={close}
          onConfirm={() => send("deploy")}
        />
      )}

      {status && (
        <ConfirmWindow
          open={dialog === "stop"}
          testId="marvel0-deploy-stop-dialog"
          title={t("marvel0.deploy.dialog.stop_title")}
          intro={sayText(
            {
              key: "marvel0.deploy.dialog.stop_intro",
              params: { unit: status.deploy?.unit ?? "?" },
            },
            t,
          )}
          estop={clientsNow ? t("marvel0.deploy.dialog.stop_client") : null}
          notes={
            site
              ? [
                  sayText(
                    {
                      key: "marvel0.deploy.dialog.stop_wait",
                      params: { s: (site.unit.stopTimeoutS ?? 0) + 30 },
                    },
                    t,
                  ),
                ]
              : []
          }
          after={
            clientsNow ? (
              <Field
                label={t("marvel0.deploy.dialog.override")}
                hint={sayText(
                  {
                    key: "marvel0.deploy.dialog.phrase_hint",
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
            ) : null
          }
          ready={stopReady}
          confirmLabel={t("marvel0.deploy.btn.stop")}
          danger
          working={pending !== null}
          refused={
            problem?.action === "stop" && dialog === "stop" ? (
              <ConfirmRefusal
                failure={problem.failure}
                testId="marvel0-deploy-stop-refused"
              >
                <WordedText
                  worded={sayStopRefusal(problem.failure, {
                    write: true,
                    site,
                  })}
                />
              </ConfirmRefusal>
            ) : null
          }
          onCancel={close}
          onConfirm={() => send("stop")}
        />
      )}
    </>
  );
}
