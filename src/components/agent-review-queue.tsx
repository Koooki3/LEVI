"use client";
import { useEffect, useId, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Image as ImageIcon,
  Merge,
  Scissors,
  X,
} from "lucide-react";
import { T, useLocale } from "./levi-locale";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Input,
  Kbd,
  Select,
  Textarea,
} from "@/components/ds";
import { Note } from "@/components/pages-ui/feedback";
import { Actions, Hint } from "./agent-ui";

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
  closedReason,
  onChange,
  onDecision,
  onEvidence,
}: {
  proposals: ReviewProposal[];
  decisions: Record<string, string>;
  disabled: boolean;
  /** Why decisions are closed (a committed change), written beside them. */
  closedReason?: string;
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
  const closedId = useId();
  const emptyId = useId();
  const why = disabled && closedReason ? closedId : undefined;
  const tone = (decision?: string) =>
    decision === "accepted"
      ? "success"
      : decision === "rejected"
        ? "danger"
        : "neutral";
  return (
    <T>
      <div className="ag-stack">
        <dl className="ag-stats">
          <div>
            <dt>Total suggestions</dt>
            <dd>{proposals.length}</dd>
          </div>
          <div>
            <dt>Accepted</dt>
            <dd>
              {Object.values(decisions).filter((d) => d === "accepted").length}
            </dd>
          </div>
          <div>
            <dt>Pending</dt>
            <dd>{proposals.length - Object.keys(decisions).length}</dd>
          </div>
        </dl>
        {disabled && closedReason && <Hint id={closedId}>{closedReason}</Hint>}
        <Field label={t("Review filter")}>
          <Select
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
          </Select>
        </Field>
        <Actions>
          <Button
            size="sm"
            icon={Check}
            disabled={disabled || !visible.length}
            aria-describedby={why ?? (!visible.length ? emptyId : undefined)}
            onClick={() =>
              onDecision(
                visible.map(({ i }) => i),
                "accepted",
              )
            }
          >
            {t("Accept visible")}
          </Button>
          <Button
            size="sm"
            icon={X}
            disabled={disabled || !visible.length}
            aria-describedby={why ?? (!visible.length ? emptyId : undefined)}
            onClick={() =>
              onDecision(
                visible.map(({ i }) => i),
                "rejected",
              )
            }
          >
            {t("Reject visible")}
          </Button>
        </Actions>
        {current ? (
          <Card padding="compact" className="ag-proposal">
            <div className="ag-proposal__nav">
              <Button
                size="sm"
                icon={ArrowLeft}
                disabled={focus <= 0}
                onClick={() => setFocus((v) => v - 1)}
              >
                {t("Previous")}
              </Button>
              <span className="ag-muted">
                {Math.min(focus + 1, visible.length)}/{visible.length} ·{" "}
                <Kbd>J</Kbd> <Kbd>K</Kbd>
              </span>
              <Button
                size="sm"
                iconEnd={ArrowRight}
                disabled={focus >= visible.length - 1}
                onClick={() => setFocus((v) => v + 1)}
              >
                {t("Next")}
              </Button>
            </div>
            <p className="ag-proposal__meta">
              <Badge tone="neutral" icon={null}>
                {t(current.p.kind)}
              </Badge>
              <span className="ag-muted">
                <T>Episode</T> {current.p.episode_index}
              </span>
              <Badge
                tone={tone(decisions[String(current.i)])}
                icon={
                  decisions[String(current.i)] === "accepted"
                    ? Check
                    : decisions[String(current.i)] === "rejected"
                      ? X
                      : undefined
                }
              >
                {t(decisions[String(current.i)] || "Pending")}
              </Badge>
            </p>
            <div className="ag-form">
              <Field label={t("Proposal text")}>
                <Textarea
                  disabled={disabled}
                  value={current.p.content}
                  onChange={(e) => edit("content", e.target.value)}
                />
              </Field>
              {current.p.subtask_id && (
                <div className="ag-grid3">
                  <Field label={t("Subtask ID")}>
                    <Input
                      disabled={disabled}
                      value={current.p.subtask_id}
                      onChange={(e) => edit("subtask_id", e.target.value)}
                    />
                  </Field>
                  <Field label={t("Attempt")}>
                    <Input
                      type="number"
                      min={1}
                      disabled={disabled}
                      value={current.p.attempt || 1}
                      onChange={(e) => edit("attempt", Number(e.target.value))}
                    />
                  </Field>
                  <Field label={t("Observed outcome")}>
                    <Select
                      disabled={disabled}
                      value={current.p.outcome || "unknown"}
                      onChange={(e) => edit("outcome", e.target.value)}
                    >
                      <option value="unknown">{t("unknown")}</option>
                      <option value="success">{t("success")}</option>
                      <option value="failure">{t("failure")}</option>
                    </Select>
                  </Field>
                </div>
              )}
              {current.p.uncertainty && (
                <Note tone="warning" role="status">
                  {current.p.uncertainty}
                </Note>
              )}
              {current.p.evidence_note && <p>{current.p.evidence_note}</p>}
              <div className="ag-grid3">
                <Field label={t("Start time")}>
                  <Input
                    type="number"
                    step="0.001"
                    min="0"
                    disabled={disabled}
                    value={current.p.start}
                    onChange={(e) => edit("start", Number(e.target.value))}
                  />
                </Field>
                {current.p.end !== null && (
                  <Field label={t("End time")}>
                    <Input
                      type="number"
                      step="0.001"
                      min="0"
                      disabled={disabled}
                      value={current.p.end}
                      onChange={(e) => edit("end", Number(e.target.value))}
                    />
                  </Field>
                )}
              </div>
            </div>
            {current.p.end !== null && (
              <Actions>
                <Button
                  size="sm"
                  icon={Scissors}
                  disabled={disabled}
                  aria-describedby={why}
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
                  {t("Split segment at midpoint")}
                </Button>
                <Button
                  size="sm"
                  icon={Merge}
                  disabled={disabled || current.i + 1 >= proposals.length}
                  aria-describedby={why}
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
                  {t("Merge next matching segment")}
                </Button>
              </Actions>
            )}
            <Actions>
              {current.p.evidence_ids.map((id, i) => (
                <Button
                  size="sm"
                  variant="ghost"
                  icon={ImageIcon}
                  key={id}
                  onClick={() => onEvidence(id)}
                >
                  <T>Evidence</T> {i + 1}
                </Button>
              ))}
              <Button
                size="sm"
                icon={Check}
                disabled={disabled}
                aria-describedby={why}
                onClick={() => {
                  onDecision([current.i], "accepted");
                  if (filter !== "pending")
                    setFocus((v) => Math.min(v + 1, visible.length - 1));
                }}
              >
                {t("Accept & next")}
              </Button>
              <Button
                size="sm"
                icon={X}
                disabled={disabled}
                aria-describedby={why}
                onClick={() => {
                  onDecision([current.i], "rejected");
                  if (filter !== "pending")
                    setFocus((v) => Math.min(v + 1, visible.length - 1));
                }}
              >
                {t("Reject & next")}
              </Button>
            </Actions>
          </Card>
        ) : (
          <div id={emptyId} role="status">
            <EmptyState
              title={t("No suggestions in this filter.")}
              description={t(
                "Review accepted items or continue to validation.",
              )}
            />
          </div>
        )}
      </div>
    </T>
  );
}
