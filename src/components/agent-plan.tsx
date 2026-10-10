"use client";
// The Agent Workbench content styles; this module is loaded with the drawer.
import "@/components/pages-ui/agent-content.css";
import { useState } from "react";
import { Check, FileText, Undo2 } from "lucide-react";
import { T, useLocale } from "./levi-locale";
import { Badge, Button, Field, Textarea } from "@/components/ds";
import { HumanActionMark, Note } from "@/components/pages-ui/feedback";
import { Actions, Disclosure, GatedButton, Hint } from "./agent-ui";
export type HarnessPlan = {
  revision: number;
  digest: string;
  approval: { actor: string } | null;
  pilot_episode: number;
  pilot_review: {
    accepted: boolean;
    note?: string;
    waived?: boolean;
  } | null;
  questions: { field: string; message: string }[];
  estimate: {
    minimum_requests: number;
    tokens: string;
    // Only a plan with workflow.event_intelligence on (docs/EVENTS.md).
    event_intelligence?: EventEstimate;
  };
  excluded: string[];
  event_intelligence?: EventSettings | null;
};
/** workflow.event_intelligence as the plan froze it. */
export type EventSettings = {
  mode: string;
  sources: string[];
  max_windows: number;
  merge_seconds: number;
  change_point_penalty: number;
  planner: string;
  active_evidence: boolean;
};
/** What event candidates add to a plan's cost: frames, never requests. */
export type EventEstimate = {
  extra_requests: number;
  max_windows_per_episode: number;
  frames_per_window: number;
  max_extra_frames_per_episode: number;
  frames_cap: number;
};
/** The plan's event intelligence settings, shown to the person approving
 * it; read-only (the settings are part of what the approval covers). */
function EventIntelligencePlan({
  settings,
  estimate,
}: {
  settings: EventSettings;
  estimate?: EventEstimate;
}) {
  const { t } = useLocale();
  return (
    <div role="group" aria-label={t("Event intelligence")}>
      <h4>{t("Event intelligence")}</h4>
      <ul className="ag-list ag-list--plain">
        <li>
          {t("Mode")}: <code className="ag-code">{settings.mode}</code>
        </li>
        <li>
          {t("Candidate sources")}:{" "}
          <code className="ag-code">{settings.sources.join(", ")}</code>
        </li>
        <li>
          {t("Candidate windows per episode, at most")}: {settings.max_windows}
        </li>
        <li>
          {t("Merge distance (s)")}: {settings.merge_seconds}
        </li>
        <li>
          {t("Change-point penalty")}: {settings.change_point_penalty}
        </li>
        <li>
          {t("Frame planner")}:{" "}
          <code className="ag-code">{settings.planner}</code>
        </li>
        <li>
          {t("Evidence a model asks for itself")}: {t("off (fixed)")}
        </li>
        {estimate && (
          <>
            <li>
              {t("Extra frames per episode, at most")}:{" "}
              {estimate.max_extra_frames_per_episode} ({t("frame cap")}{" "}
              {estimate.frames_cap})
            </li>
            <li>
              {t("Extra model requests")}: {estimate.extra_requests}
            </li>
          </>
        )}
      </ul>
      <Hint>
        {t(
          "Event candidates from the recorded signals only choose extra refinement frames within the frame cap; they are never boundaries. Their priorities are uncalibrated heuristics, not probabilities.",
        )}
      </Hint>
    </div>
  );
}
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
        <Note tone="warning" role="status">
          Legacy plan: create a new plan before execution.
        </Note>
      </T>
    );
  const needsNote = !note.trim();
  return (
    <T>
      <section className="ag-section" aria-label={t("Execution plan")}>
        <h3>{t("Execution plan")}</h3>
        <p>
          <Badge tone={plan.approval ? "success" : "warning"}>
            {t(
              plan.approval
                ? "Execution approved"
                : "Awaiting execution approval",
            )}
          </Badge>{" "}
          <span className="ag-muted">v{plan.revision}</span>
        </p>
        <p>
          {plan.pilot_review?.waived ? (
            <strong>
              <T>No separate pilot: this plan waives it</T>
            </strong>
          ) : (
            <>
              <T>Pilot episode</T>: {plan.pilot_episode}
            </>
          )}{" "}
          · <T>Minimum model requests</T>: {plan.estimate.minimum_requests}
        </p>
        <Hint>
          Token cost is unknown until pilot. Limits are enforced; estimates are
          not price promises.
        </Hint>
        <Hint>
          Execution approval does not authorize publishing or overwriting
          annotations.
        </Hint>
        {plan.event_intelligence && (
          <EventIntelligencePlan
            settings={plan.event_intelligence}
            estimate={plan.estimate.event_intelligence}
          />
        )}
        <Disclosure summary={t("Contract details")} icon={FileText}>
          <code className="ag-code">{plan.digest}</code>
          <ul className="ag-list">
            {plan.excluded.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        </Disclosure>
        {plan.questions.map((q) => (
          <Note tone="warning" role="alert" key={q.field}>
            {q.message}
          </Note>
        ))}
        {!plan.approval && (
          <Actions>
            <HumanActionMark />
            <GatedButton
              size="sm"
              variant="primary"
              icon={Check}
              disabled={busy}
              reason={
                plan.questions.length
                  ? t("Answer the questions above to approve this plan.")
                  : null
              }
              onClick={onApprove}
            >
              {t("Approve execution plan")}
            </GatedButton>
          </Actions>
        )}
        {plan.approval &&
          !plan.pilot_review?.waived &&
          completed.includes(plan.pilot_episode) &&
          draftRevision !== undefined && (
            <>
              <Field label={t("Pilot review notes")}>
                <Textarea
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder={t(
                    "Check labels, boundaries, uncertainty and observed cost",
                  )}
                />
              </Field>
              <Actions>
                <HumanActionMark />
                <Button
                  size="sm"
                  variant="primary"
                  icon={Check}
                  disabled={busy || needsNote}
                  aria-describedby={
                    needsNote ? "agent-pilot-note-why" : undefined
                  }
                  onClick={() => onPilot(true, note)}
                >
                  {t("Accept pilot quality")}
                </Button>
                <Button
                  size="sm"
                  icon={Undo2}
                  disabled={busy || needsNote}
                  aria-describedby={
                    needsNote ? "agent-pilot-note-why" : undefined
                  }
                  onClick={() => onPilot(false, note)}
                >
                  {t("Needs plan revision")}
                </Button>
              </Actions>
              {needsNote && (
                <Hint id="agent-pilot-note-why">
                  {t("Write a review note to accept or reject the pilot.")}
                </Hint>
              )}
              {plan.pilot_review && (
                <Note
                  tone={plan.pilot_review.accepted ? "success" : "warning"}
                  role="status"
                >
                  {t(
                    plan.pilot_review.accepted
                      ? "Pilot accepted"
                      : "Pilot rejected; bulk execution blocked",
                  )}
                  : {plan.pilot_review.note}
                </Note>
              )}
            </>
          )}
      </section>
    </T>
  );
}
