"use client";
// The initial-state check asked of the operator (`scene_check:
// operator_attested`): the frames the system just grabbed and, for each
// required predicate, a true / false / cannot-tell answer. Only the answers
// and the question's own ids are sent; the question is answered once.
import { ScanEye } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button, Radio } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { automaticApi } from "./api";
import { errorText } from "./launch-panel";
import { Frames } from "./pending-card";
import {
  sceneAnswers,
  sceneComplete,
  sceneSecondsLeft,
  type SceneAnswer,
} from "./run-logic";
import { Note } from "./run-parts";
import type { SceneQuestion } from "./types";

const OPTIONS: { id: string; value: SceneAnswer; key: string }[] = [
  { id: "true", value: true, key: "automatic.run.scene.true" },
  { id: "false", value: false, key: "automatic.run.scene.false" },
  { id: "null", value: null, key: "automatic.run.scene.unclear" },
];

export function SceneCheck({
  runId,
  question,
  onAnswered,
}: {
  runId: string;
  question: SceneQuestion;
  onAnswered?: () => void;
}) {
  const { t } = useLocale();
  const [chosen, setChosen] = useState<Record<string, SceneAnswer | undefined>>(
    {},
  );
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const inFlight = useRef(false);
  const id = question.request_id;

  // A new question (new request id) starts clean.
  useEffect(() => {
    setChosen({});
    setSubmitted(false);
    setError("");
  }, [id]);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const names = question.predicates.map((predicate) => predicate.name);
  const required = question.predicates
    .filter((predicate) => predicate.required)
    .map((predicate) => predicate.name);
  const left = sceneSecondsLeft(question.asked_at, question.timeout_s, now);
  const expired = left <= 0;
  const locked = submitted || expired;

  const submit = async () => {
    if (inFlight.current || locked || !sceneComplete(required, chosen)) return;
    inFlight.current = true;
    setError("");
    try {
      await automaticApi.sceneAnswer(
        runId,
        {
          request_id: question.request_id,
          nonce: question.nonce,
          frames_sha256: question.frames_sha256,
        },
        sceneAnswers(names, chosen),
      );
      setSubmitted(true);
      onAnswered?.();
    } catch (caught) {
      setError(errorText(caught, t));
    } finally {
      inFlight.current = false;
    }
  };

  return (
    <section className="ar-card" aria-labelledby="ar-scene-title">
      <h2 id="ar-scene-title">
        <ScanEye aria-hidden="true" width={20} height={20} />
        {t("automatic.run.scene.title")}
      </h2>
      <p>{t("automatic.run.scene.intro")}</p>
      <Frames runId={runId} shas={question.frames} />
      {question.predicates.map((predicate) => (
        <div className="ar-scene-row" key={predicate.name}>
          <fieldset disabled={locked}>
            <legend>
              {predicate.text}
              {predicate.required ? (
                ""
              ) : (
                <span className="ar-muted">
                  {" "}
                  ({t("automatic.run.scene.optional")})
                </span>
              )}
            </legend>
            {OPTIONS.map((option) => (
              <Radio
                key={option.id}
                name={`${id}-${predicate.name}`}
                label={t(option.key)}
                checked={chosen[predicate.name] === option.value}
                onChange={() =>
                  setChosen((old) => ({
                    ...old,
                    [predicate.name]: option.value,
                  }))
                }
              />
            ))}
          </fieldset>
        </div>
      ))}
      {expired && !submitted && (
        <Note tone="warning" role="status">
          {t("automatic.run.scene.expired")}
        </Note>
      )}
      {submitted && (
        <Note tone="info" role="status">
          {t("automatic.run.scene.sent")}
        </Note>
      )}
      {error && (
        <Note tone="danger" role="alert">
          {error}
        </Note>
      )}
      <div className="ar-actions">
        <Button
          variant="primary"
          disabled={locked || !sceneComplete(required, chosen)}
          onClick={() => void submit()}
        >
          {t("automatic.run.scene.submit")}
        </Button>
        {!locked && (
          <span className="ar-muted">
            {t("automatic.run.scene.left").replace("{n}", String(left))}
          </span>
        )}
      </div>
    </section>
  );
}
