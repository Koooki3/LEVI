"use client";
import { useEffect, useState } from "react";
import { T, useLocale } from "./levi-locale";

export type ReviewProposal = {
  episode_index: number;
  kind: string;
  content: string;
  start: number;
  end: number | null;
  evidence_ids: string[];
  subtask_id?: string | null;
  attempt?: number;
  outcome?: string | null;
  uncertainty?: string;
  evidence_note?: string;
};
export default function AgentReviewQueue({
  proposals,
  decisions,
  disabled,
  onChange,
  onDecision,
  onEvidence,
}: {
  proposals: ReviewProposal[];
  decisions: Record<string, string>;
  disabled: boolean;
  onChange: (proposals: ReviewProposal[]) => void;
  onDecision: (indices: number[], decision: "accepted" | "rejected") => void;
  onEvidence: (id: string) => void;
}) {
  const { t } = useLocale();
  const [filter, setFilter] = useState("pending");
  const [focus, setFocus] = useState(0);
  const visible = proposals
    .map((p, i) => ({ p, i }))
    .filter(
      ({ p, i }) =>
        filter === "all" ||
        (filter === "pending" ? !decisions[String(i)] : p.kind === filter),
    );
  const current = visible[Math.min(focus, Math.max(0, visible.length - 1))];
  useEffect(() => {
    if (current?.p.evidence_ids[0]) onEvidence(current.p.evidence_ids[0]);
    // The parent callback is intentionally not a dependency: selecting a card
    // must seek once, not on every background status refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.i, current?.p.evidence_ids[0]]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (
        target.closest("input,textarea,select,[contenteditable=true]") ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey
      )
        return;
      if (event.key === "j")
        setFocus((v) => Math.max(0, Math.min(v + 1, visible.length - 1)));
      if (event.key === "k") setFocus((v) => Math.max(0, v - 1));
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [visible.length]);
  function edit(field: string, value: string | number) {
    if (!current) return;
    onChange(
      proposals.map((p, i) => (i === current.i ? { ...p, [field]: value } : p)),
    );
  }
  return (
    <T>
      <div className="levi-review-queue">
        <div className="levi-agent-budget">
          <div>
            <strong>{proposals.length}</strong>
            <p>Total suggestions</p>
          </div>
          <div>
            <strong>
              {Object.values(decisions).filter((d) => d === "accepted").length}
            </strong>
            <p>Accepted</p>
          </div>
          <div>
            <strong>{proposals.length - Object.keys(decisions).length}</strong>
            <p>Pending</p>
          </div>
        </div>
        <label>
          Review filter
          <select
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              setFocus(0);
            }}
          >
            <option value="pending">{t("Pending")}</option>
            <option value="all">{t("All suggestions")}</option>
            <option value="issue">{t("Issues")}</option>
            <option value="outcome">{t("Outcome suggestions")}</option>
            <option value="segment">{t("Segments")}</option>
            <option value="event">{t("Events")}</option>
          </select>
        </label>
        <div className="levi-agent-actions">
          <button
            disabled={disabled || !visible.length}
            onClick={() =>
              onDecision(
                visible.map(({ i }) => i),
                "accepted",
              )
            }
          >
            Accept visible
          </button>
          <button
            disabled={disabled || !visible.length}
            onClick={() =>
              onDecision(
                visible.map(({ i }) => i),
                "rejected",
              )
            }
          >
            Reject visible
          </button>
        </div>
        {current ? (
          <article>
            <div className="levi-agent-actions">
              <button
                disabled={focus <= 0}
                onClick={() => setFocus((v) => v - 1)}
              >
                Previous
              </button>
              <span>
                {Math.min(focus + 1, visible.length)}/{visible.length} · J / K
              </span>
              <button
                disabled={focus >= visible.length - 1}
                onClick={() => setFocus((v) => v + 1)}
              >
                Next
              </button>
            </div>
            <small>
              {t(current.p.kind)} · <T>Episode</T> {current.p.episode_index} ·{" "}
              {t(decisions[String(current.i)] || "Pending")}
            </small>
            <label>
              Proposal text
              <textarea
                disabled={disabled}
                value={current.p.content}
                onChange={(e) => edit("content", e.target.value)}
              />
            </label>
            {current.p.subtask_id && (
              <div className="levi-agent-budget">
                <label>
                  Subtask ID
                  <input
                    disabled={disabled}
                    value={current.p.subtask_id}
                    onChange={(e) => edit("subtask_id", e.target.value)}
                  />
                </label>
                <label>
                  Attempt
                  <input
                    type="number"
                    min={1}
                    disabled={disabled}
                    value={current.p.attempt || 1}
                    onChange={(e) => edit("attempt", Number(e.target.value))}
                  />
                </label>
                <label>
                  Observed outcome
                  <select
                    disabled={disabled}
                    value={current.p.outcome || "unknown"}
                    onChange={(e) => edit("outcome", e.target.value)}
                  >
                    <option value="unknown">{t("unknown")}</option>
                    <option value="success">{t("success")}</option>
                    <option value="failure">{t("failure")}</option>
                  </select>
                </label>
              </div>
            )}
            {current.p.uncertainty && (
              <p role="status">{current.p.uncertainty}</p>
            )}
            {current.p.evidence_note && <p>{current.p.evidence_note}</p>}
            <div className="levi-agent-budget">
              <label>
                Start time
                <input
                  type="number"
                  step="0.001"
                  min="0"
                  disabled={disabled}
                  value={current.p.start}
                  onChange={(e) => edit("start", Number(e.target.value))}
                />
              </label>
              {current.p.end !== null && (
                <label>
                  End time
                  <input
                    type="number"
                    step="0.001"
                    min="0"
                    disabled={disabled}
                    value={current.p.end}
                    onChange={(e) => edit("end", Number(e.target.value))}
                  />
                </label>
              )}
            </div>
            {current.p.end !== null && (
              <div className="levi-agent-actions">
                <button
                  disabled={disabled}
                  onClick={() => {
                    const midpoint = (current.p.start + current.p.end!) / 2;
                    onChange(
                      proposals.flatMap((p, i) =>
                        i === current.i
                          ? [
                              { ...p, end: midpoint },
                              { ...p, start: midpoint },
                            ]
                          : [p],
                      ),
                    );
                  }}
                >
                  Split segment at midpoint
                </button>
                <button
                  disabled={disabled || current.i + 1 >= proposals.length}
                  onClick={() => {
                    const next = proposals[current.i + 1];
                    if (
                      next.episode_index !== current.p.episode_index ||
                      next.end === null ||
                      next.subtask_id !== current.p.subtask_id
                    )
                      return;
                    onChange(
                      proposals.flatMap((p, i) =>
                        i === current.i
                          ? [
                              {
                                ...p,
                                start: Math.min(p.start, next.start),
                                end: Math.max(p.end!, next.end!),
                                evidence_ids: Array.from(
                                  new Set([
                                    ...p.evidence_ids,
                                    ...next.evidence_ids,
                                  ]),
                                ),
                              },
                            ]
                          : i === current.i + 1
                            ? []
                            : [p],
                      ),
                    );
                  }}
                >
                  Merge next matching segment
                </button>
              </div>
            )}
            <div className="levi-agent-actions">
              {current.p.evidence_ids.map((id, i) => (
                <button key={id} onClick={() => onEvidence(id)}>
                  <T>Evidence</T> {i + 1}
                </button>
              ))}
              <button
                disabled={disabled}
                onClick={() => {
                  onDecision([current.i], "accepted");
                  if (filter !== "pending")
                    setFocus((v) => Math.min(v + 1, visible.length - 1));
                }}
              >
                Accept & next
              </button>
              <button
                disabled={disabled}
                onClick={() => {
                  onDecision([current.i], "rejected");
                  if (filter !== "pending")
                    setFocus((v) => Math.min(v + 1, visible.length - 1));
                }}
              >
                Reject & next
              </button>
            </div>
          </article>
        ) : (
          <p role="status">
            No suggestions in this filter. Review accepted items or continue to
            validation.
          </p>
        )}
      </div>
    </T>
  );
}
