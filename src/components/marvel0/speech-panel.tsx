"use client";
// The voice card of the marvel0, above the task instruction of the Client
// command card: hold a button, say a task instruction, see which allowed
// instruction it was matched to, and press Use to put THAT sentence (never the
// transcript) into the task instruction field. Use fills one field. Making the
// command and copying it stay the two separate presses they were, and nothing
// here starts the client or the arm: voice cannot stop or pause the robot, and
// the card says so next to the microphone at all times.
import { Fragment, useId, type ReactNode } from "react";
import { Mic, RefreshCw } from "lucide-react";
import {
  Badge,
  Button,
  Field,
  Input,
  Radio,
  RadioGroup,
  type Tone,
} from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { Note, Problem } from "@/components/pages-ui/feedback";
import { FailureDetail, WordedText } from "./action-parts";
import { sayText } from "./marvel0-say";
import type { Say } from "./marvel0-logic";
import { speechApi, type SpeechApi } from "./speech-api";
import {
  MAX_SECONDS,
  candidateOf,
  describeCandidate,
  engineSay,
  failureWords,
  heardSay,
  languageSay,
  micSay,
  proposalView,
  sayParts,
  topChoices,
  type EngineState,
  type Flag,
  type Problem as SpeechProblem,
  type Proposal,
} from "./speech-logic";
import { useSpeechSession, type SpeechSession } from "./speech-session";
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
}: {
  api?: SpeechApi;
  /** The model of the form: it decides which instructions are allowed. */
  modelId: string;
  /** Receives one sentence of the allowed list when a person presses Use. */
  onUse: (prompt: string) => void;
  /** Replaces the microphone capture (tests). */
  createCapture?: (events: CaptureEvents) => CaptureLike;
}) {
  const { t } = useLocale();
  const session = useSpeechSession({ api, modelId, onUse, createCapture });
  const { state, availability: access } = session;
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

      {state.problem && <ProblemView problem={state.problem} />}
      {state.phase === "idle" && state.notice && (
        <NoticeView notice={state.notice} />
      )}
      {state.proposal && (
        <ProposalCard session={session} proposal={state.proposal} />
      )}
    </section>
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

function ProposalCard({
  session,
  proposal,
}: {
  session: SpeechSession;
  proposal: Proposal;
}) {
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
      {view === "V" ? (
        <FlagBanner flag={proposal.match.flag} />
      ) : (
        <Choices session={session} proposal={proposal} view={view} />
      )}
      <p className="rg-muted">{t("marvel0.speech.expires")}</p>
    </section>
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
}: {
  session: SpeechSession;
  proposal: Proposal;
  view: "A" | "B" | "list";
}) {
  const { t } = useLocale();
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
        <p className="rg-muted">{t("marvel0.speech.read_first")}</p>
        <div className="rg-actions-row">
          <Button
            variant="primary"
            disabled={blocked}
            data-testid="marvel0-speech-use"
            onClick={() => session.use(best.prompt)}
          >
            {t("marvel0.speech.btn.use")}
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
      <p className="rg-muted">{t("marvel0.speech.read_first")}</p>
      <div className="rg-actions-row">
        <Button
          variant="primary"
          disabled={blocked || proposal.picked === null}
          data-testid="marvel0-speech-use"
          onClick={() => proposal.picked && session.use(proposal.picked)}
        >
          {t("marvel0.speech.btn.use_selected")}
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
