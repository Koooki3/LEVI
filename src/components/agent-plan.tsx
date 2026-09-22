"use client";
import { useState } from "react";
import { T, useLocale } from "./levi-locale";
export type HarnessPlan = {
  revision: number;
  digest: string;
  approval: { actor: string } | null;
  pilot_episode: number;
  pilot_review: { accepted: boolean; note: string } | null;
  questions: { field: string; message: string }[];
  estimate: { minimum_requests: number; tokens: string };
  excluded: string[];
};
export default function AgentPlan({
  plan,
  busy,
  completed,
  draftRevision,
  onApprove,
  onPilot,
}: {
  plan?: HarnessPlan;
  busy: boolean;
  completed: number[];
  draftRevision?: number;
  onApprove: () => void;
  onPilot: (accepted: boolean, note: string) => void;
}) {
  const { t } = useLocale();
  const [note, setNote] = useState("");
  if (!plan)
    return (
      <T>
        <p role="status">Legacy plan: create a new plan before execution.</p>
      </T>
    );
  return (
    <T>
      <section className="levi-review-queue" aria-label="Execution plan">
        <h3>Execution plan</h3>
        <p>
          <strong>
            {t(
              plan.approval
                ? "Execution approved"
                : "Awaiting execution approval",
            )}
          </strong>{" "}
          · v{plan.revision}
        </p>
        <p>
          <T>Pilot episode</T>: {plan.pilot_episode} ·{" "}
          <T>Minimum model requests</T>: {plan.estimate.minimum_requests}
        </p>
        <p className="levi-agent-muted">
          Token cost is unknown until pilot. Limits are enforced; estimates are
          not price promises.
        </p>
        <p>
          Execution approval does not authorize publishing or overwriting
          annotations.
        </p>
        <details>
          <summary>Contract details</summary>
          <code>{plan.digest}</code>
          <ul>
            {plan.excluded.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        </details>
        {plan.questions.map((q) => (
          <p role="alert" key={q.field}>
            {q.message}
          </p>
        ))}
        {!plan.approval && (
          <button
            disabled={busy || !!plan.questions.length}
            onClick={onApprove}
          >
            Approve execution plan
          </button>
        )}
        {plan.approval &&
          completed.includes(plan.pilot_episode) &&
          draftRevision !== undefined && (
            <>
              <label>
                Pilot review notes
                <textarea
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder={t(
                    "Check labels, boundaries, uncertainty and observed cost",
                  )}
                />
              </label>
              <div className="levi-agent-actions">
                <button
                  disabled={busy || !note.trim()}
                  onClick={() => onPilot(true, note)}
                >
                  Accept pilot quality
                </button>
                <button
                  disabled={busy || !note.trim()}
                  onClick={() => onPilot(false, note)}
                >
                  Needs plan revision
                </button>
              </div>
              {!note.trim() && (
                <p className="levi-agent-muted">
                  {t("Write a review note to accept or reject the pilot.")}
                </p>
              )}
              {plan.pilot_review && (
                <p role="status">
                  {t(
                    plan.pilot_review.accepted
                      ? "Pilot accepted"
                      : "Pilot rejected; bulk execution blocked",
                  )}
                  : {plan.pilot_review.note}
                </p>
              )}
            </>
          )}
      </section>
    </T>
  );
}
