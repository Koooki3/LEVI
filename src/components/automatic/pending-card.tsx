"use client";
// "You are needed": the card shown while a run waits for a person (a reset by
// hand, an unknown scene). It tells what the run saw, takes the operator's
// success/failure label and offers the two-step resume.
//
// Blindness: the automatic verdict and how the last episode ended (an early
// stop is a hint of the verdict) are not rendered until the operator has
// labelled the episode (`operator_label.hidden_until_labelled` is false).
import { EyeOff, Hand, TriangleAlert } from "lucide-react";
import { useRef, useState } from "react";
import { Button, Checkbox, Dialog } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { AutomaticApiError, automaticApi, frameUrl, newRequestId } from "./api";
import { errorText } from "./launch-panel";
import {
  LABELS,
  LABEL_KEY,
  decisionText,
  localDuration,
  isBlind,
  resultDone,
  resultKey,
  visibleEnding,
  visibleVerdict,
} from "./run-logic";
import { Note } from "./run-parts";
import type {
  CommandResult,
  OperatorLabel,
  PendingCard as PendingCardData,
} from "./types";

const REASON_KEY: Record<string, string> = {
  scene_reset_required: "automatic.run.reason.scene_reset_required",
  scene_unknown: "automatic.run.reason.scene_unknown",
  operator_stop: "automatic.run.reason.operator_stop",
  scene_unavailable: "automatic.run.reason.scene_unavailable",
};
const ENDED_KEY: Record<string, string> = {
  early_stop: "automatic.run.ended.early_stop",
  horizon: "automatic.run.ended.horizon",
  horizon_exhausted: "automatic.run.ended.horizon",
  operator_stop: "automatic.run.ended.operator_stop",
};
/** A refusal arriving as an HTTP error (code from the body). */
const RESUME_ERRORS: Record<string, string> = {
  stale_sequence: "automatic.run.result.stale_sequence",
  confirmations_missing: "automatic.run.result.confirmations_missing",
  not_waiting: "automatic.run.result.not_waiting",
  not_running: "automatic.run.result.not_running",
};
/** Results after which the same command id must not be sent again. */
const ROTATE = new Set([
  "stale_sequence",
  "confirmations_missing",
  "not_waiting",
]);

export function Frames({ runId, shas }: { runId: string; shas: string[] }) {
  const { t } = useLocale();
  return (
    <div className="ar-frames">
      {shas.map((sha, index) => {
        const url = frameUrl(runId, sha);
        return url ? (
          // The frame is a same-origin JPEG from the run's evidence folder.
          // eslint-disable-next-line @next/next/no-img-element
          <img
            key={sha}
            src={url}
            loading="lazy"
            alt={t("automatic.run.frame").replace("{n}", String(index + 1))}
          />
        ) : null;
      })}
    </div>
  );
}

/** The two-step resume: the operator declares the scene is handled and the
 * health rechecked; the system then checks the scene again on its own. */
export function ResumeDialog({
  runId,
  expectedSeq,
  challenge,
  onClose,
  onDone,
}: {
  runId: string;
  expectedSeq: number;
  challenge: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useLocale();
  const [handled, setHandled] = useState(false);
  const [rechecked, setRechecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<CommandResult | null>(null);
  const [error, setError] = useState("");
  // One command id for the whole dialog: a second press (or a retry after a
  // lost answer) is the same command. It changes only after the system said
  // "no" to it, so a corrected attempt is a new command.
  const commandId = useRef(newRequestId());
  const inFlight = useRef(false);

  const submit = async () => {
    if (inFlight.current || !handled || !rechecked) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      const got = await automaticApi.resume(runId, {
        commandId: commandId.current,
        expectedSeq,
        environmentHandled: handled,
        healthRechecked: rechecked,
        challenge,
      });
      setResult(got);
      if (resultDone(got)) {
        onDone();
        onClose();
      } else if (got.code && ROTATE.has(got.code))
        commandId.current = newRequestId();
    } catch (caught) {
      // A refusal the server wrote down (HTTP 409) frees the id; a lost
      // answer keeps it.
      if (caught instanceof AutomaticApiError && caught.status === 409)
        commandId.current = newRequestId();
      setError(errorText(caught, t, RESUME_ERRORS));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };
  const message = resultKey(resultDone(result) ? null : result);
  return (
    <Dialog
      open
      onClose={onClose}
      title={t("automatic.run.resume.title")}
      description={t("automatic.run.resume.detail")}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!handled || !rechecked}
            onClick={() => void submit()}
          >
            {t("automatic.run.resume.confirm")}
          </Button>
        </>
      }
    >
      <div className="ar-dialog-body">
        <Checkbox
          label={t("automatic.run.resume.handled")}
          checked={handled}
          disabled={busy}
          onChange={(event) => setHandled(event.target.checked)}
        />
        <Checkbox
          label={t("automatic.run.resume.rechecked")}
          checked={rechecked}
          disabled={busy}
          onChange={(event) => setRechecked(event.target.checked)}
        />
        <p className="ar-muted">{t("automatic.run.resume.second_step")}</p>
        {(!handled || !rechecked) && (
          <p className="ar-muted">{t("automatic.run.resume.need_both")}</p>
        )}
        {message && (
          <Note tone="warning" role="alert">
            {t(message)}
          </Note>
        )}
        {error && (
          <Note tone="danger" role="alert">
            {error}
          </Note>
        )}
      </div>
    </Dialog>
  );
}
export function PendingCard({
  runId,
  card,
  challenge,
  onLabelled,
  onChanged,
}: {
  runId: string;
  card: PendingCardData;
  challenge: string;
  /** The card the label call returned (shown until a poll catches up). */
  onLabelled: (card: PendingCardData) => void;
  onChanged: () => void;
}) {
  const { t } = useLocale();
  const [posting, setPosting] = useState<OperatorLabel | null>(null);
  const [error, setError] = useState("");
  const [resuming, setResuming] = useState(false);
  const inFlight = useRef(false);
  const blind = isBlind(card);
  const episode = card.last_episode;
  const ending = visibleEnding(card);
  const verdict = visibleVerdict(card);

  const label = async (value: OperatorLabel) => {
    if (!episode || inFlight.current) return;
    inFlight.current = true;
    setPosting(value);
    setError("");
    try {
      const got = await automaticApi.label(runId, episode.episode_id, value);
      if (got.card) onLabelled(got.card);
      onChanged();
    } catch (caught) {
      setError(errorText(caught, t));
    } finally {
      inFlight.current = false;
      setPosting(null);
    }
  };

  const reasonKey = REASON_KEY[card.reason];
  return (
    <section
      className="ar-card"
      aria-labelledby="ar-needed-title"
      role="region"
    >
      <h2 id="ar-needed-title">
        <Hand aria-hidden="true" width={20} height={20} />
        {t("automatic.run.needed.title")}
      </h2>
      <p>
        {reasonKey ? t(reasonKey) : <code>{card.reason}</code>}{" "}
        <span className="ar-muted">
          {t("automatic.run.needed.waited")
            .replace("{time}", localDuration(card.waited_ms, t))
            .replace("{n}", String(card.nth_wait))}
        </span>
      </p>

      {episode && (
        <section aria-labelledby="ar-last-title">
          <h3 id="ar-last-title">{t("automatic.run.last.title")}</h3>
          <dl className="ar-dl">
            <dt>{t("automatic.run.last.episode")}</dt>
            <dd className="ar-code">{episode.episode_id}</dd>
            {ending.endedBy && (
              <>
                <dt>{t("automatic.run.last.ended_by")}</dt>
                <dd>
                  {ENDED_KEY[ending.endedBy]
                    ? t(ENDED_KEY[ending.endedBy])
                    : ending.endedBy}
                </dd>
              </>
            )}
            {ending.stopReason && (
              <>
                <dt>{t("automatic.run.last.stop_reason")}</dt>
                <dd>{ending.stopReason}</dd>
              </>
            )}
            {ending.rolloutLabel && (
              <>
                <dt>{t("automatic.run.last.rollout_label")}</dt>
                <dd>{ending.rolloutLabel}</dd>
              </>
            )}
            {verdict && (
              <>
                <dt>{t("automatic.run.last.verdict")}</dt>
                <dd>{verdict}</dd>
              </>
            )}
          </dl>
          {blind && (
            <Note
              tone="info"
              icon={EyeOff}
              title={t("automatic.run.blind.title")}
            >
              {t("automatic.run.blind.detail")}
            </Note>
          )}
          <div
            className="ar-label-buttons"
            role="group"
            aria-label={t("automatic.run.label.group")}
          >
            {LABELS.map((value) => (
              <Button
                key={value}
                variant={value === "success" ? "primary" : "secondary"}
                aria-pressed={card.operator_label.current === value}
                loading={posting === value}
                disabled={posting !== null}
                onClick={() => void label(value)}
              >
                {t(LABEL_KEY[value])}
              </Button>
            ))}
          </div>
          {card.operator_label.current && (
            <p className="ar-muted">
              {t("automatic.run.label.current").replace(
                "{label}",
                t(LABEL_KEY[card.operator_label.current]),
              )}
            </p>
          )}
          {error && (
            <Note tone="danger" role="alert">
              {error}
            </Note>
          )}
        </section>
      )}

      <section aria-labelledby="ar-contract-title">
        <h3 id="ar-contract-title">{t("automatic.run.contract.title")}</h3>
        <p>
          <code>{card.contract.id_version}</code>
        </p>
        {card.contract.status === "draft" && (
          <Note
            tone="warning"
            icon={TriangleAlert}
            title={t("automatic.run.isc.draft")}
            role="status"
          >
            {t("automatic.run.isc.draft_detail")}
          </Note>
        )}
        {card.contract.predicates.length > 0 && (
          <ul className="ar-predicates">
            {card.contract.predicates.map((text) => (
              <li key={text}>{text}</li>
            ))}
          </ul>
        )}
      </section>

      {card.assessment && (
        <section aria-labelledby="ar-assess-title">
          <h3 id="ar-assess-title">{t("automatic.run.assess.title")}</h3>
          <dl className="ar-dl">
            <dt>{t("automatic.run.assess.decision")}</dt>
            <dd>{decisionText(card.assessment.decision, t)}</dd>
            {card.assessment.failed.length > 0 && (
              <>
                <dt>{t("automatic.run.assess.failed")}</dt>
                <dd>{card.assessment.failed.join(", ")}</dd>
              </>
            )}
            {card.assessment.unknown.length > 0 && (
              <>
                <dt>{t("automatic.run.assess.unknown")}</dt>
                <dd>{card.assessment.unknown.join(", ")}</dd>
              </>
            )}
          </dl>
          <Frames runId={runId} shas={card.assessment.frame_refs} />
        </section>
      )}

      <div className="ar-actions">
        <Button variant="primary" onClick={() => setResuming(true)}>
          {t("automatic.run.resume.open")}
        </Button>
      </div>
      {resuming && (
        <ResumeDialog
          runId={runId}
          expectedSeq={card.resume_seq}
          challenge={challenge}
          onClose={() => setResuming(false)}
          onDone={onChanged}
        />
      )}
    </section>
  );
}
