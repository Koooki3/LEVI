"use client";
// The voice card of the marvel0, above the task instruction of the Client
// command card: hold a button, say a task instruction, see which allowed
// instruction it was matched to, and press Use to put THAT sentence (never the
// transcript) into the task instruction field. Use fills one field. Making the
// command and copying it stay the two separate presses they were, and nothing
// here starts the client or the arm: voice cannot stop or pause the robot, and
// the card says so next to the microphone at all times. The one exception is
// the voice run (voice-run.ts), which the person switches on here through a
// confirm window: then a clear sentence is used by itself after a short
// countdown with Cancel, and Use runs the sentence instead of filling it in.
// Beside the matched and generated sentences the card always offers the
// sentence as heard (tidied, `HeardChoice`): a Use press of its own, never the
// default and never by itself, even in the voice run.
import {
  Fragment,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Mic, Play, RefreshCw } from "lucide-react";
import {
  Badge,
  Button,
  Field,
  Input,
  Radio,
  RadioGroup,
  Select,
  type Tone,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, Problem } from "@/components/pages-ui/feedback";
import { FailureDetail, WordedText } from "./action-parts";
import { ConfirmWindow } from "./confirm-window";
import { wordOf } from "./ctl-text";
import { CHANGE_ACKS } from "./run-panel";
import type { VoiceRun, VoiceRunPhase } from "./use-voice-run";
import {
  GATE_WAIT_MS,
  VOICE_RUN_DELAY_MS,
  autoPick,
  type NoAutoWhy,
} from "./voice-run";

const GATE_WAIT_S = GATE_WAIT_MS / 1000;
import { sayText } from "./marvel0-say";
import type { Say } from "./marvel0-logic";
import { speechApi, type SpeechApi } from "./speech-api";
import {
  MAX_SECONDS,
  candidateOf,
  describeCandidate,
  effectiveTier,
  engineSay,
  failureWords,
  generatedPrompts,
  heardOption,
  heardSay,
  heardWhySay,
  languageSay,
  micSay,
  proposalView,
  sayParts,
  timingSay,
  topChoices,
  type EngineState,
  type Flag,
  type Generated,
  type GeneratedStatus,
  type Problem as SpeechProblem,
  type Proposal,
  type TranslationRefusal,
  type UntranslatedWhy,
} from "./speech-logic";
import {
  useSpeechSession,
  type MicPicker,
  type SpeechSession,
} from "./speech-session";
import type { CaptureEvents, CaptureLike } from "./voice-capture";

const ENGINE_TONE: Record<EngineState, Tone> = {
  unavailable: "neutral",
  off: "neutral",
  loading: "info",
  ready: "success",
  busy: "info",
  failed: "danger",
};

/** A sentence of the catalogue whose values (from the transcript or the server)
 * are set apart in <bdi>, so text from outside cannot reorder what is around it. */
function SayLine({ say }: { say: Say }) {
  const { t } = useLocale();
  if (!("key" in say)) return <>{sayText(say, t)}</>;
  const parts = sayParts(t(say.key), say.params, (nested) =>
    sayText(nested, t),
  );
  return (
    <>
      {parts.map((part, index) =>
        part.value ? (
          <bdi key={index}>{part.text}</bdi>
        ) : (
          <Fragment key={index}>{part.text}</Fragment>
        ),
      )}
    </>
  );
}

export function SpeechPanel({
  api = speechApi,
  modelId,
  onUse,
  createCapture,
  voiceRun = null,
}: {
  api?: SpeechApi;
  /** The model of the form: it decides which instructions are allowed. */
  modelId: string;
  /** Receives one sentence of the allowed list when a person presses Use. */
  onUse: (prompt: string) => void;
  /** Replaces the microphone capture (tests). */
  createCapture?: (events: CaptureEvents) => CaptureLike;
  /** The page's voice run; without it the card has no such switch. */
  voiceRun?: VoiceRun | null;
}) {
  const { t } = useLocale();
  const session = useSpeechSession({ api, modelId, onUse, createCapture });
  const { state, availability: access } = session;
  const running = voiceRun?.on === true;
  const armedAt = running ? voiceRun!.armedAt : null;
  const titleId = useId();
  const reasonsId = useId();
  const recording = state.phase === "recording";
  const micProblem =
    session.mic === "insecure_context" || session.mic === "unsupported"
      ? session.mic
      : null;
  const usable = access.usable && session.mic === "ok";
  const engine = session.read.kind === "ok" ? session.read.status : null;
  const reasons: Array<{ key: string; node: ReactNode }> = [
    ...(micProblem
      ? [{ key: micProblem, node: sayText(micSay(micProblem), t) }]
      : []),
    ...access.reasons.map((worded, index) => ({
      key: `r${index}`,
      node: <WordedText worded={worded} />,
    })),
  ];
  const busyKey =
    state.phase === "recording"
      ? state.opening
        ? "marvel0.speech.state.opening"
        : "marvel0.speech.state.recording"
      : state.phase === "uploading"
        ? "marvel0.speech.state.uploading"
        : state.proposal?.checking
          ? "marvel0.speech.state.checking"
          : null;

  return (
    <section
      className="rg-speech"
      data-testid="marvel0-speech"
      data-phase={state.phase}
      aria-labelledby={titleId}
      onKeyDown={(event) => {
        // Enter in a field of this card never submits the form around it
        // (that would press "Make the command"); in the text to match it
        // matches again.
        if (
          event.nativeEvent.isComposing ||
          event.key !== "Enter" ||
          !(event.target instanceof HTMLInputElement)
        )
          return;
        event.preventDefault();
        if (event.target.dataset.speechDraft === "true")
          session.rematch(event.target.value);
      }}
    >
      <div className="rg-actions-row">
        <h4 id={titleId}>{t("marvel0.speech.title")}</h4>
        {engine && (
          <Badge tone={ENGINE_TONE[engine.state]}>
            {sayText(engineSay(engine.state), t)}
          </Badge>
        )}
      </div>
      <p className="rg-muted">{t("marvel0.speech.scope")}</p>
      <div data-testid="marvel0-speech-estop">
        <Note tone="warning">{t("marvel0.speech.no_estop")}</Note>
      </div>
      {voiceRun && <VoiceRunSwitch voiceRun={voiceRun} />}

      {!micProblem && (
        <MicField
          mics={session.mics}
          disabled={recording || state.phase === "uploading"}
        />
      )}

      <div className="rg-speech__talk">
        <Button
          ref={session.attach}
          className="rg-speech__ptt"
          variant={recording ? "primary" : "secondary"}
          icon={Mic}
          disabled={
            !usable || state.phase === "uploading" || !!state.proposal?.checking
          }
          aria-pressed={recording}
          aria-describedby={reasons.length ? reasonsId : undefined}
          data-testid="marvel0-speech-mic"
        >
          {t(
            recording
              ? "marvel0.speech.btn.recording"
              : "marvel0.speech.btn.talk",
          )}
        </Button>
        <span className="rg-speech__meter" aria-hidden="true">
          <span ref={session.meter} className="rg-speech__meter-bar" />
        </span>
        {session.micOpen && (
          <span className="rg-muted" data-testid="marvel0-speech-mic-on">
            {t("marvel0.speech.mic_on")}
          </span>
        )}
      </div>
      <p className="rg-muted">
        {sayText({ key: "marvel0.speech.how", params: { n: MAX_SECONDS } }, t)}
      </p>

      {reasons.length > 0 && (
        <div id={reasonsId} data-testid="marvel0-speech-reasons">
          <ul className="rg-notes">
            {reasons.map((reason) => (
              <li key={reason.key}>{reason.node}</li>
            ))}
          </ul>
          <Button size="sm" icon={RefreshCw} onClick={session.recheck}>
            {t("marvel0.speech.btn.recheck")}
          </Button>
        </div>
      )}
      {access.install && (
        <p className="rg-warn" data-testid="marvel0-speech-install">
          {t("marvel0.speech.install")}
        </p>
      )}
      {engine?.state === "ready" && state.phase !== "recording" && (
        <div className="rg-actions-row">
          <Button size="sm" variant="ghost" onClick={session.release}>
            {t("marvel0.speech.btn.release")}
          </Button>
          <span className="rg-muted">{t("marvel0.speech.release_hint")}</span>
        </div>
      )}

      <div
        role="status"
        className="rg-speech__status"
        data-testid="marvel0-speech-status"
      >
        {busyKey
          ? t(busyKey)
          : state.proposal
            ? t("marvel0.speech.review_ready")
            : ""}
      </div>

      {state.timing &&
        !state.proposal?.checking &&
        (state.phase === "proposal" || state.phase === "idle") && (
          <p className="rg-muted" data-testid="marvel0-speech-timing">
            {sayText(timingSay(state.timing), t)}
          </p>
        )}

      {state.problem && <ProblemView problem={state.problem} />}
      {state.phase === "idle" && state.notice && (
        <NoticeView notice={state.notice} />
      )}
      {state.proposal && (
        <ProposalCard
          session={session}
          proposal={state.proposal}
          armedAt={armedAt}
        />
      )}
    </section>
  );
}

/** Which microphone a press opens: the browser's default or one of the
 * inputs, remembered in this browser. It also says which input the last
 * opening used, so a camera's microphone taken by mistake shows. */
function MicField({ mics, disabled }: { mics: MicPicker; disabled: boolean }) {
  const { t } = useLocale();
  const select = useRef<HTMLSelectElement>(null);
  const name = (label: string, index: number) =>
    label ||
    sayText(
      { key: "marvel0.speech.mic.pick.unnamed", params: { n: index + 1 } },
      t,
    );
  const noName = { key: "marvel0.speech.mic.pick.no_name" };
  /** A choice made by a button that then goes away: focus stays on the list. */
  const chooseFromButton = (id: string | null) => {
    mics.choose(id);
    select.current?.focus();
  };
  // A switch that was undone by the input going away again is told by the
  // "not connected" note instead.
  const switched = mics.missing ? null : mics.switched;
  const restored = mics.missing ? null : mics.restored;
  return (
    <div className="rg-speech__mic" data-testid="marvel0-speech-mic-pick">
      <Field
        label={t("marvel0.speech.mic.pick.label")}
        hint={t(
          mics.hidden
            ? "marvel0.speech.mic.pick.hidden"
            : "marvel0.speech.mic.pick.hint",
        )}
      >
        <Select
          ref={select}
          value={mics.chosen ?? ""}
          disabled={disabled}
          data-testid="marvel0-speech-mic-select"
          onChange={(event) => mics.choose(event.target.value || null)}
        >
          <option value="">{t("marvel0.speech.mic.pick.default")}</option>
          {mics.options.map((input, index) => (
            <option key={input.id} value={input.id}>
              {name(input.label, index)}
            </option>
          ))}
        </Select>
      </Field>
      {/* One live region that stays mounted, so a note that appears in it is
          announced (a region mounted together with its text often is not). */}
      <div role="status" data-testid="marvel0-speech-mic-status">
        {switched && (
          <div data-testid="marvel0-speech-mic-switched">
            <Note tone="info">
              <SayLine
                say={{
                  key: "marvel0.speech.mic.pick.switched",
                  params: {
                    label: switched.to.label || noName,
                    previous: switched.from.label || noName,
                  },
                }}
              />
            </Note>
          </div>
        )}
        {restored && (
          <p className="rg-muted" data-testid="marvel0-speech-mic-restored">
            <SayLine
              say={{
                key: "marvel0.speech.mic.pick.restored",
                params: { label: restored.label || noName },
              }}
            />
          </p>
        )}
        {mics.offers.map((input) => (
          <p
            key={input.id}
            className="rg-muted"
            data-testid="marvel0-speech-mic-offer"
          >
            <SayLine
              say={{
                key: "marvel0.speech.mic.pick.offer",
                params: { label: input.label || noName },
              }}
            />
          </p>
        ))}
        {mics.missing && (
          <div data-testid="marvel0-speech-mic-missing">
            <Note tone="warning">
              <SayLine
                say={{
                  key: "marvel0.speech.mic.pick.missing",
                  params: { label: mics.missing.label || noName },
                }}
              />
            </Note>
          </div>
        )}
        {mics.inUse &&
          (mics.inUse.fallback ? (
            <div data-testid="marvel0-speech-mic-fallback">
              <Note tone="warning">
                <SayLine
                  say={{
                    key: "marvel0.speech.mic.pick.fallback",
                    params: { label: mics.inUse.label || noName },
                  }}
                />
              </Note>
            </div>
          ) : (
            <p className="rg-muted" data-testid="marvel0-speech-mic-in-use">
              <SayLine
                say={{
                  key: "marvel0.speech.mic.pick.in_use",
                  params: { label: mics.inUse.label || noName },
                }}
              />
            </p>
          ))}
        {mics.quietDb !== null && (
          <div data-testid="marvel0-speech-mic-quiet">
            <Note tone="warning">
              {sayText(
                {
                  key: "marvel0.speech.mic.pick.quiet",
                  params: { db: Math.round(mics.quietDb) },
                },
                t,
              )}
            </Note>
          </div>
        )}
      </div>
      {(switched || mics.offers.length > 0 || mics.missing) && (
        <div className="rg-actions-row">
          {mics.offers.map((input, index) => (
            <Button
              key={input.id}
              size="sm"
              disabled={disabled}
              data-testid="marvel0-speech-mic-use-new"
              onClick={() => chooseFromButton(input.id)}
            >
              <SayLine
                say={{
                  key: "marvel0.speech.mic.pick.use_new",
                  params: { label: name(input.label, index) },
                }}
              />
            </Button>
          ))}
          {mics.offers.length > 0 && (
            <Button
              size="sm"
              variant="ghost"
              data-testid="marvel0-speech-mic-dismiss"
              onClick={() => {
                mics.dismiss();
                select.current?.focus();
              }}
            >
              {t("marvel0.speech.mic.pick.dismiss")}
            </Button>
          )}
          {(switched || mics.missing) && (
            <Button
              size="sm"
              variant="ghost"
              disabled={disabled}
              data-testid="marvel0-speech-mic-forget"
              onClick={() => chooseFromButton(null)}
            >
              {t("marvel0.speech.mic.pick.forget")}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function NoticeView({
  notice,
}: {
  notice: NonNullable<SpeechSession["state"]["notice"]>;
}) {
  const { t } = useLocale();
  if (notice.kind === "used")
    return (
      <div data-testid="marvel0-speech-notice" data-kind="used">
        <Note tone="success" role="status">
          {sayText(
            {
              key: "marvel0.speech.used",
              params: { make: { key: "marvel0.cmd.btn.make" } },
            },
            t,
          )}
        </Note>
      </div>
    );
  return (
    <p
      className="rg-muted"
      role="status"
      data-testid="marvel0-speech-notice"
      data-kind={notice.kind}
    >
      {notice.kind === "expired"
        ? t("marvel0.speech.expired")
        : sayText(heardSay(notice.reason), t)}
    </p>
  );
}

function ProblemView({ problem }: { problem: SpeechProblem }) {
  const { t } = useLocale();
  if (problem.kind === "mic")
    return (
      <div data-testid="marvel0-speech-problem" data-kind={problem.code}>
        <Problem
          tone="warning"
          title={t("marvel0.speech.problem.mic")}
          why={sayText(micSay(problem.code), t)}
        />
      </div>
    );
  return (
    <div data-testid="marvel0-speech-problem" data-kind="api">
      <Problem
        title={t("marvel0.err.speech.title")}
        why={
          problem.kind === "format" ? (
            t("marvel0.err.speech.format")
          ) : (
            <>
              <WordedText worded={failureWords(problem.failure)} />
              <FailureDetail failure={problem.failure} />
            </>
          )
        }
      />
    </div>
  );
}

const FLAG_WORDS: Record<Flag, { title: string; body: string }> = {
  stop: {
    title: "marvel0.speech.flag.stop.title",
    body: "marvel0.speech.flag.stop.body",
  },
  negation: {
    title: "marvel0.speech.flag.negation.title",
    body: "marvel0.speech.flag.negation.body",
  },
  unsafe: {
    title: "marvel0.speech.flag.unsafe.title",
    body: "marvel0.speech.flag.unsafe.body",
  },
};

/** The voice run's switch: off, a button that opens the confirm window with
 * the start window's three ticks; on, the warning that says what it does and
 * the button that turns it off. Below, what the last sentence did. */
function VoiceRunSwitch({ voiceRun }: { voiceRun: VoiceRun }) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const seconds = VOICE_RUN_DELAY_MS / 1000;
  const status = voiceRun.status;
  return (
    <div
      className="rg-speech__run"
      data-testid="marvel0-speech-run"
      data-on={voiceRun.on}
    >
      <div className="rg-actions-row">
        <h4>{t("marvel0.speech.run.title")}</h4>
        {voiceRun.on ? (
          <Button
            size="sm"
            data-testid="marvel0-speech-run-off"
            onClick={voiceRun.disarm}
          >
            {t("marvel0.speech.run.btn.off")}
          </Button>
        ) : (
          <Button
            size="sm"
            icon={Play}
            data-testid="marvel0-speech-run-on"
            onClick={() => setOpen(true)}
          >
            {t("marvel0.speech.run.btn.on")}
          </Button>
        )}
      </div>
      {voiceRun.on ? (
        <div data-testid="marvel0-speech-run-warning">
          <Note tone="warning">
            {sayText(
              {
                key: "marvel0.speech.run.on",
                params: { s: seconds, gate: GATE_WAIT_S },
              },
              t,
            )}
          </Note>
        </div>
      ) : (
        <p className="rg-muted">
          {sayText(
            { key: "marvel0.speech.run.off_hint", params: { s: seconds } },
            t,
          )}
        </p>
      )}
      <div role="status" data-testid="marvel0-speech-run-status">
        {status && (
          <p
            className={RUN_PROGRESS.has(status.phase) ? "rg-muted" : "rg-warn"}
            data-phase={status.phase}
          >
            <SayLine
              say={{
                key: RUN_STATUS_KEY[status.phase],
                params: {
                  prompt: { raw: status.prompt },
                  s: GATE_WAIT_S,
                },
              }}
            />
            {status.why && (
              <>
                {" "}
                <WordedText worded={status.why} />
              </>
            )}
            {status.dropped && <> {t("marvel0.speech.run.dropped")}</>}
          </p>
        )}
      </div>
      <ConfirmWindow
        open={open}
        testId="marvel0-speech-run-dialog"
        title={t("marvel0.speech.run.dialog.title")}
        intro={sayText(
          { key: "marvel0.speech.run.dialog.intro", params: { s: seconds } },
          t,
        )}
        estop={t("marvel0.run.start.estop")}
        hazards={[
          t("marvel0.speech.run.dialog.route"),
          sayText(
            {
              key: "marvel0.speech.run.dialog.enter",
              params: { gate: GATE_WAIT_S },
            },
            t,
          ),
        ]}
        notes={[
          t("marvel0.speech.run.dialog.ticks"),
          t("marvel0.run.notice.gates_scope"),
        ]}
        attest={CHANGE_ACKS.map((name) => ({
          code: name,
          label: wordOf(t, "marvel0.run.ack.", name),
        }))}
        confirmLabel={t("marvel0.speech.run.dialog.go")}
        confirmIcon={Play}
        onCancel={() => setOpen(false)}
        onConfirm={(ticked) => {
          voiceRun.arm({
            estop: ticked.estop === true,
            scene_reset: ticked.scene_reset === true,
            area_clear: ticked.area_clear === true,
          });
          setOpen(false);
        }}
      />
    </div>
  );
}

const RUN_STATUS_KEY: Record<VoiceRunPhase, string> = {
  starting: "marvel0.speech.run.status.starting",
  changing: "marvel0.speech.run.status.changing",
  waiting: "marvel0.speech.run.status.waiting",
  entering: "marvel0.speech.run.status.entering",
  entered: "marvel0.speech.run.status.entered",
  refused: "marvel0.speech.run.status.refused",
  start_failed: "marvel0.speech.run.status.start_failed",
  enter_failed: "marvel0.speech.run.status.enter_failed",
  late: "marvel0.speech.run.status.late",
  gone: "marvel0.speech.run.status.gone",
  held: "marvel0.speech.run.status.held",
  off_held: "marvel0.speech.run.status.off_held",
  off_pending: "marvel0.speech.run.status.off_pending",
};
/** The phases that are progress, not a sentence that did not run. */
const RUN_PROGRESS = new Set<VoiceRunPhase>([
  "starting",
  "changing",
  "waiting",
  "entering",
  "entered",
]);

const NO_AUTO_KEY: Record<NoAutoWhy, string> = {
  vetoed: "marvel0.speech.run.no_auto.vetoed",
  edited: "marvel0.speech.run.no_auto.edited",
  truncated: "marvel0.speech.run.no_auto.truncated",
  guessed: "marvel0.speech.run.no_auto.guessed",
  disagree: "marvel0.speech.run.no_auto.disagree",
  choose: "marvel0.speech.run.no_auto.choose",
  person: "marvel0.speech.run.no_auto.person",
  earlier: "marvel0.speech.run.no_auto.earlier",
};

/** With the voice run on: the sentence a clear proposal runs by itself after
 * VOICE_RUN_DELAY_MS on screen, with Cancel; or why it waits for a Use press.
 * The press it makes is the same `use` as the person's, with the same checks
 * (on screen 800 ms, not expired, the allowed list unchanged). Cancel also
 * revokes that use while its check is still on its way. */
function AutoRun({
  session,
  proposal,
  armedAt,
}: {
  session: SpeechSession;
  proposal: Proposal;
  armedAt: number;
}) {
  const { t } = useLocale();
  const [cancelled, setCancelled] = useState<number | null>(null);
  const pick = autoPick(proposal, armedAt);
  const prompt = "prompt" in pick ? pick.prompt : null;
  const delayMs = "prompt" in pick ? pick.delayMs : VOICE_RUN_DELAY_MS;
  const translated = "prompt" in pick ? pick.translated : [];
  const live = prompt !== null && cancelled !== proposal.id;
  const use = session.use;
  const { id, shownAt, checking } = proposal;
  useEffect(() => {
    if (!live || prompt === null || checking) return;
    const timer = setTimeout(
      () => use(prompt),
      Math.max(0, shownAt + delayMs - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [live, prompt, checking, use, id, shownAt, delayMs]);
  if (prompt === null)
    return (
      <p
        className="rg-muted"
        data-testid="marvel0-speech-run-no-auto"
        data-why={"why" in pick ? pick.why : undefined}
      >
        {t(NO_AUTO_KEY["why" in pick ? pick.why : "choose"])}
      </p>
    );
  if (!live)
    return (
      <p className="rg-muted" data-testid="marvel0-speech-run-cancelled">
        {t("marvel0.speech.run.cancelled")}
      </p>
    );
  return (
    <div className="rg-speech__auto" data-testid="marvel0-speech-run-auto">
      <Note tone="warning">
        <SayLine
          say={
            translated.length > 0
              ? {
                  key: "marvel0.speech.run.auto_translated",
                  params: {
                    s: delayMs / 1000,
                    prompt: { raw: prompt },
                    words: { raw: translated.join(", ") },
                  },
                }
              : {
                  key: "marvel0.speech.run.auto",
                  params: { s: delayMs / 1000, prompt: { raw: prompt } },
                }
          }
        />
      </Note>
      <div className="rg-actions-row">
        <Button
          variant="primary"
          data-testid="marvel0-speech-run-cancel"
          onClick={() => {
            session.cancelUse();
            setCancelled(id);
          }}
        >
          {t("marvel0.speech.run.btn.cancel")}
        </Button>
      </div>
    </div>
  );
}

function ProposalCard({
  session,
  proposal,
  armedAt,
}: {
  session: SpeechSession;
  proposal: Proposal;
  /** When the voice run was switched on; null while it is off. While it is
   * on, Use runs the sentence. */
  armedAt: number | null;
}) {
  const running = armedAt !== null;
  const { t } = useLocale();
  const view = proposalView(proposal);
  const language = proposal.language;
  return (
    <section
      className="rg-speech__proposal"
      data-testid="marvel0-speech-proposal"
      data-view={view}
      data-tier={proposal.match.tier}
      aria-busy={proposal.checking || undefined}
    >
      <p className="rg-muted">
        {proposal.edited ? (
          t("marvel0.speech.edited")
        ) : (
          <SayLine
            say={{
              key: "marvel0.speech.heard_as",
              params: {
                language: languageSay(language),
                model: proposal.model ?? "?",
              },
            }}
          />
        )}
      </p>
      <p
        className="rg-speech__heard"
        data-testid="marvel0-speech-transcript"
        lang={
          language === "English"
            ? "en"
            : language === "Chinese"
              ? "zh"
              : undefined
        }
      >
        <bdi>{proposal.transcript}</bdi>
      </p>
      {proposal.truncated && (
        <p className="rg-muted">
          {sayText(
            { key: "marvel0.speech.truncated", params: { n: MAX_SECONDS } },
            t,
          )}
        </p>
      )}
      <TranscriptEditor session={session} proposal={proposal} />
      {armedAt !== null && (
        <AutoRun session={session} proposal={proposal} armedAt={armedAt} />
      )}
      {view === "V" ? (
        <FlagBanner flag={proposal.match.flag} />
      ) : (
        <>
          <HeardChoice
            session={session}
            proposal={proposal}
            running={running}
          />
          <GeneratedChoice
            session={session}
            proposal={proposal}
            running={running}
          />
          <Choices
            session={session}
            proposal={proposal}
            view={view}
            running={running}
          />
        </>
      )}
      <p className="rg-muted">{t("marvel0.speech.expires")}</p>
    </section>
  );
}

/** The sentence as the person said it, tidied (lower case, no ending
 * punctuation), always offered first when something was heard. It is an extra
 * option: the default stays the matched or generated sentence, and it has a
 * Use press of its own that nothing else, the voice run's countdown included,
 * ever makes. It is disabled, with the reason, for a restricted model and when
 * the task instruction field would refuse it (the server's check). */
function HeardChoice({
  session,
  proposal,
  running,
}: {
  session: SpeechSession;
  proposal: Proposal;
  running: boolean;
}) {
  const { t } = useLocale();
  const option = heardOption(proposal.match);
  if (!option) return null;
  const blocked = !option.usable || !proposal.armed || proposal.checking;
  // The main button when nothing else is on offer to use.
  const alone =
    generatedPrompts(proposal.match).length === 0 &&
    proposal.match.tier !== "A" &&
    proposal.match.tier !== "B";
  return (
    <div
      className="rg-speech__generated"
      data-testid="marvel0-speech-asheard"
      data-usable={option.usable}
    >
      <p className="rg-muted">{t("marvel0.speech.asheard.title")}</p>
      <p
        className="rg-speech__prompt"
        lang={option.chinese ? "zh" : "en"}
        data-testid="marvel0-speech-asheard-prompt"
      >
        <bdi>{option.prompt}</bdi>
      </p>
      {option.why ? (
        <p className="rg-warn" data-testid="marvel0-speech-asheard-why">
          {sayText(heardWhySay(option.why), t)}
        </p>
      ) : (
        <p className="rg-muted">{t("marvel0.speech.asheard.note")}</p>
      )}
      {option.usable && option.chinese && (
        <p className="rg-warn" data-testid="marvel0-speech-asheard-chinese">
          {t("marvel0.speech.asheard.chinese")}
        </p>
      )}
      {option.usable && running && (
        <p className="rg-muted" data-testid="marvel0-speech-asheard-no-auto">
          {t("marvel0.speech.asheard.no_auto")}
        </p>
      )}
      <div className="rg-actions-row">
        <Button
          variant={alone ? "primary" : "secondary"}
          disabled={blocked}
          data-testid="marvel0-speech-use-asheard"
          onClick={() => session.use(option.prompt)}
        >
          {t(
            running
              ? "marvel0.speech.btn.run_asheard"
              : "marvel0.speech.btn.use_asheard",
          )}
        </Button>
      </div>
    </div>
  );
}

/** Why there is no generated sentence (an ok, vetoed or restricted result
 * never shows this line). */
const GENERATED_WHY: Record<
  Exclude<GeneratedStatus, "ok" | "vetoed" | "restricted">,
  string
> = {
  no_task: "marvel0.speech.generated.why.no_task",
  unsupported: "marvel0.speech.generated.why.unsupported",
  no_frame: "marvel0.speech.generated.why.no_frame",
  untranslated: "marvel0.speech.generated.why.untranslated",
  unclear: "marvel0.speech.generated.why.unclear",
};

/** The sentence written in the datasets' most common phrasing, above the
 * matched instructions. It is labelled as generated (not an instruction the
 * policy was trained on) and handed over only by its own Use press. */
function GeneratedChoice({
  session,
  proposal,
  running,
}: {
  session: SpeechSession;
  proposal: Proposal;
  running: boolean;
}) {
  const { t } = useLocale();
  const generated = proposal.match.generated;
  if (
    !generated ||
    effectiveTier(proposal.match) === "V" ||
    proposal.match.family !== "all" ||
    generated.status === "vetoed" ||
    generated.status === "restricted"
  )
    return null;
  const usable = generatedPrompts(proposal.match);
  if (generated.status !== "ok") {
    const status = generated.status;
    return (
      <>
        <p className="rg-muted" data-testid="marvel0-speech-generated-none">
          {sayText(
            {
              key: GENERATED_WHY[status],
              params: { names: generated.untranslated.join(", ") },
            },
            t,
          )}
        </p>
        <TranslationRefusals generated={generated} />
      </>
    );
  }
  if (!generated.prompt || usable.length === 0) return null;
  const blocked = !proposal.armed || proposal.checking;
  const prompt = generated.prompt;
  const original = generated.datasetOriginal;
  return (
    <div
      className="rg-speech__generated"
      data-testid="marvel0-speech-generated"
    >
      <p className="rg-muted">{t("marvel0.speech.generated.title")}</p>
      <p
        className="rg-speech__prompt"
        lang="en"
        data-testid="marvel0-speech-generated-prompt"
      >
        <bdi>{prompt}</bdi>
      </p>
      <p className="rg-muted">
        {sayText(
          {
            key: "marvel0.speech.generated.note",
            params: {
              frame: generated.frames.join(" then "),
              n: generated.episodes,
            },
          },
          t,
        )}
      </p>
      {generated.repairs.length > 0 && (
        <p className="rg-muted" data-testid="marvel0-speech-generated-repairs">
          {sayText(
            {
              key: "marvel0.speech.generated.guessed",
              params: {
                words: generated.repairs
                  .map((repair) => `${repair.heard} → ${repair.as}`)
                  .join(", "),
              },
            },
            t,
          )}
        </p>
      )}
      {generated.slots.some((slot) => slot.source === "english") && (
        <p className="rg-muted" data-testid="marvel0-speech-generated-slots">
          {sayText(
            {
              key: "marvel0.speech.generated.outside",
              params: {
                words: generated.slots
                  .filter((slot) => slot.source === "english")
                  .map((slot) => slot.english)
                  .join(", "),
              },
            },
            t,
          )}
        </p>
      )}
      {generated.slots.some((slot) => slot.source === "translation") && (
        <p
          className="rg-muted"
          data-testid="marvel0-speech-generated-translated"
        >
          {sayText(
            {
              key: "marvel0.speech.generated.translated",
              params: {
                words: generated.slots
                  .filter((slot) => slot.source === "translation")
                  .map((slot) => {
                    const back = generated.translations.find(
                      (item) => item.ok && item.said === slot.said,
                    )?.back;
                    return back
                      ? `${slot.said} → ${slot.english} (→ ${back})`
                      : `${slot.said} → ${slot.english}`;
                  })
                  .join(", "),
              },
            },
            t,
          )}
        </p>
      )}
      <div className="rg-actions-row">
        <Button
          variant="primary"
          disabled={blocked}
          data-testid="marvel0-speech-use-generated"
          onClick={() => session.use(prompt)}
        >
          {t(
            running
              ? "marvel0.speech.btn.run_generated"
              : "marvel0.speech.btn.use_generated",
          )}
        </Button>
      </div>
      {original && usable.includes(original) && (
        <>
          <p className="rg-muted">{t("marvel0.speech.generated.original")}</p>
          <p
            className="rg-speech__prompt"
            lang="en"
            data-testid="marvel0-speech-generated-original"
          >
            <bdi>{original}</bdi>
          </p>
          <div className="rg-actions-row">
            <Button
              disabled={blocked}
              data-testid="marvel0-speech-use-original"
              onClick={() => session.use(original)}
            >
              {t(
                running
                  ? "marvel0.speech.btn.run_original"
                  : "marvel0.speech.btn.use_original",
              )}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

const REFUSAL_TEXT: Record<TranslationRefusal, string> = {
  not_object: "marvel0.speech.generated.refused.not_object",
  known_as: "marvel0.speech.generated.refused.known_as",
  unchecked: "marvel0.speech.generated.refused.unchecked",
};
const NOT_ASKED_TEXT: Record<UntranslatedWhy, string> = {
  off: "marvel0.speech.generated.not_asked.off",
  not_installed: "marvel0.speech.generated.not_asked.not_installed",
  too_many: "marvel0.speech.generated.not_asked.too_many",
  blocked: "marvel0.speech.generated.not_asked.blocked",
  not_loaded: "marvel0.speech.generated.not_asked.not_loaded",
  busy: "marvel0.speech.generated.not_asked.busy",
  rate_limit: "marvel0.speech.generated.not_asked.rate_limit",
  gpu_memory: "marvel0.speech.generated.not_asked.gpu_memory",
  no_time: "marvel0.speech.generated.not_asked.no_time",
  failed: "marvel0.speech.generated.not_asked.failed",
};

/** Why each Chinese object name got no English: the local model's answer and
 * the check it failed, or why the model was not asked at all. */
function TranslationRefusals({ generated }: { generated: Generated }) {
  const { t } = useLocale();
  // After a failed generation every answer is shown, a passed one too: the
  // sentence may have failed for another reason.
  const shown = generated.translations;
  if (shown.length === 0 && !generated.untranslatedWhy) return null;
  return (
    <ul className="rg-muted" data-testid="marvel0-speech-generated-refused">
      {shown.map((item) => (
        <li key={item.said} data-refusal={item.reason ?? "none"}>
          {sayText(
            {
              key: item.ok
                ? "marvel0.speech.generated.refused.passed"
                : item.reason === "unchecked" && !item.back
                  ? "marvel0.speech.generated.refused.no_back"
                  : REFUSAL_TEXT[item.reason ?? "not_object"],
              params: {
                said: item.said,
                english: item.english ?? "",
                back: item.back ?? "",
                known: item.knownAs.join(", "),
              },
            },
            t,
          )}
        </li>
      ))}
      {generated.untranslatedWhy && (
        <li data-testid="marvel0-speech-generated-not-asked">
          {sayText({ key: NOT_ASKED_TEXT[generated.untranslatedWhy] }, t)}
        </li>
      )}
    </ul>
  );
}

function FlagBanner({ flag }: { flag: Flag | null }) {
  const { t } = useLocale();
  const words = FLAG_WORDS[flag ?? "unsafe"];
  return (
    <div data-testid="marvel0-speech-flag" data-flag={flag ?? "unsafe"}>
      <Problem title={t(words.title)} why={t(words.body)} />
    </div>
  );
}

function TranscriptEditor({
  session,
  proposal,
}: {
  session: SpeechSession;
  proposal: Proposal;
}) {
  const { t } = useLocale();
  if (!proposal.editing)
    return (
      <div className="rg-actions-row">
        <Button
          size="sm"
          variant="ghost"
          disabled={proposal.checking}
          onClick={() => session.edit(true)}
        >
          {t("marvel0.speech.btn.edit")}
        </Button>
      </div>
    );
  return (
    <div className="rg-speech__edit">
      <Field
        label={t("marvel0.speech.edit.label")}
        hint={t("marvel0.speech.edit.hint")}
      >
        <Input
          value={proposal.draft}
          maxLength={300}
          autoComplete="off"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          data-speech-draft="true"
          onChange={(event) => session.draft(event.target.value)}
        />
      </Field>
      <div className="rg-actions-row">
        <Button
          size="sm"
          disabled={proposal.checking || !proposal.draft.trim()}
          onClick={() => session.rematch(proposal.draft)}
        >
          {t("marvel0.speech.btn.match_again")}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => session.edit(false)}>
          {t("marvel0.speech.btn.cancel")}
        </Button>
      </div>
    </div>
  );
}

function Differences({
  proposal,
  prompt,
  inline = false,
}: {
  proposal: Proposal;
  prompt: string;
  /** Inside a radio's description, where a list is not allowed. */
  inline?: boolean;
}) {
  const candidate = candidateOf(proposal.match, prompt);
  const lines = candidate ? describeCandidate(candidate) : [];
  if (lines.length === 0) return null;
  const items = lines.map((line, index) => ({ line, index }));
  if (inline)
    return (
      <span
        className="rg-speech__diffs"
        data-testid="marvel0-speech-differences"
      >
        {items.map(({ line, index }) => (
          <span key={index}>
            <SayLine say={line} />
          </span>
        ))}
      </span>
    );
  return (
    <ul className="rg-notes" data-testid="marvel0-speech-differences">
      {items.map(({ line, index }) => (
        <li key={index}>
          <SayLine say={line} />
        </li>
      ))}
    </ul>
  );
}

function Choices({
  session,
  proposal,
  view,
  running,
}: {
  session: SpeechSession;
  proposal: Proposal;
  view: "A" | "B" | "list";
  running: boolean;
}) {
  const { t } = useLocale();
  const readFirst = (
    <p className="rg-muted">
      {t(
        running ? "marvel0.speech.run.read_first" : "marvel0.speech.read_first",
      )}
    </p>
  );
  const name = useId();
  const blocked = !proposal.armed || proposal.checking;
  const best = proposal.match.best;
  const family =
    proposal.match.family === "restricted" ? (
      <p className="rg-muted">{t("marvel0.speech.family.restricted")}</p>
    ) : null;

  if (view === "A" && best)
    return (
      <div data-testid="marvel0-speech-tier-a">
        <p className="rg-muted">{t("marvel0.speech.a.title")}</p>
        <p
          className="rg-speech__prompt"
          lang="en"
          data-testid="marvel0-speech-best"
        >
          <bdi>{best.prompt}</bdi>
        </p>
        <Differences proposal={proposal} prompt={best.prompt} />
        {family}
        {readFirst}
        <div className="rg-actions-row">
          <Button
            variant="primary"
            disabled={blocked}
            data-testid="marvel0-speech-use"
            onClick={() => session.use(best.prompt)}
          >
            {t(running ? "marvel0.speech.btn.run" : "marvel0.speech.btn.use")}
          </Button>
          <Button
            onClick={session.showAll}
            data-testid="marvel0-speech-not-this"
          >
            {t("marvel0.speech.btn.not_this")}
          </Button>
        </div>
      </div>
    );

  const prompts =
    view === "B" ? topChoices(proposal.match) : proposal.match.allowed;
  const legend =
    view === "B"
      ? "marvel0.speech.b.title"
      : proposal.match.tier === "C" && !proposal.showAll
        ? "marvel0.speech.c.title"
        : "marvel0.speech.all.title";
  return (
    <div
      data-testid={
        view === "B" ? "marvel0-speech-tier-b" : "marvel0-speech-tier-list"
      }
    >
      {view === "list" && proposal.match.tier === "C" && !proposal.showAll && (
        <p className="rg-muted">{t("marvel0.speech.c.hint")}</p>
      )}
      {prompts.length === 0 ? (
        <p className="rg-muted">{t("marvel0.speech.empty_list")}</p>
      ) : (
        <RadioGroup legend={t(legend)} className="rg-speech__choices">
          {prompts.map((prompt) => (
            <Radio
              key={prompt}
              name={name}
              value={prompt}
              checked={proposal.picked === prompt}
              disabled={proposal.checking}
              onChange={() => session.pick(prompt)}
              label={<bdi lang="en">{prompt}</bdi>}
              description={
                view === "B" ? (
                  <Differences proposal={proposal} prompt={prompt} inline />
                ) : undefined
              }
            />
          ))}
        </RadioGroup>
      )}
      {family}
      {readFirst}
      <div className="rg-actions-row">
        <Button
          variant="primary"
          disabled={blocked || proposal.picked === null}
          data-testid="marvel0-speech-use"
          onClick={() => proposal.picked && session.use(proposal.picked)}
        >
          {t(
            running
              ? "marvel0.speech.btn.run_selected"
              : "marvel0.speech.btn.use_selected",
          )}
        </Button>
        {view === "B" && (
          <Button onClick={session.showAll} data-testid="marvel0-speech-none">
            {t("marvel0.speech.btn.none")}
          </Button>
        )}
      </div>
    </div>
  );
}
