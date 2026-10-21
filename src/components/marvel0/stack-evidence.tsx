"use client";
// The passive side of the arm stack, folded under the controls: what each
// member of the stack was seen doing and from which sources, the dry run of
// the start gates of A, B and C ("would start" or "would refuse", with each
// check), the health monitor's automatic management, and the stack lock.
// Read-only: nothing here sends a request; the controls above act. It shows
// the document of `GET /api/levi/marvel0/stack` the S4-0 module defined
// (stack-status-logic.ts).
import { Badge } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";
import { sayText } from "./marvel0-say";
import type { StackView } from "./stack-status-logic";

export function StackEvidence({ view }: { view: StackView }) {
  const { t } = useLocale();
  return (
    <details
      className="rg-details rg-evidence"
      data-testid="marvel0-stack-evidence"
    >
      <summary>{t("marvel0.ctl.evidence.title")}</summary>
      <p data-testid="marvel0-stack-verdict" data-verdict={view.verdict}>
        {t("marvel0.stack.verdict.title")}:{" "}
        <Badge tone={view.verdict === "busy" ? "warning" : "neutral"}>
          {sayText(view.verdictLabel, t)}
        </Badge>
      </p>
      <dl className="rg-facts" data-testid="marvel0-stack-members">
        {view.members.map((row) => (
          <div
            key={row.id}
            className="rg-fact"
            data-member={row.id}
            data-state={row.state}
          >
            <dt>
              {sayText(row.name, t)}{" "}
              <Badge tone={row.tone}>{sayText(row.stateLabel, t)}</Badge>
            </dt>
            <dd>
              {sayText(row.why, t)}
              {row.evidence.length > 0 && (
                <ul className="rg-notes">
                  {row.evidence.map((e, index) => (
                    <li key={index}>
                      {sayText(e.label, t)}: {sayText(e.value, t)}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </div>
        ))}
      </dl>

      <h4 className="rg-evidence__h">{t("marvel0.stack.gates.title")}</h4>
      <p className="rg-muted">{t("marvel0.ctl.evidence.gates_note")}</p>
      <div data-testid="marvel0-stack-gates">
        {view.gates.map((gate) => (
          <details
            key={gate.id}
            data-gate={gate.id}
            data-verdict={gate.verdict}
          >
            <summary>
              {sayText(gate.name, t)}:{" "}
              <Badge
                tone={gate.verdict === "would_start" ? "success" : "warning"}
              >
                {sayText(gate.verdictLabel, t)}
              </Badge>
            </summary>
            <ul className="rg-notes">
              {gate.checks.map((check) => (
                <li key={check.code} data-outcome={check.outcome}>
                  {sayText(check.label, t)}: {sayText(check.outcomeLabel, t)}
                </li>
              ))}
            </ul>
          </details>
        ))}
      </div>

      <h4 className="rg-evidence__h">{t("marvel0.stack.monitor.title")}</h4>
      <p>
        {t(
          view.monitor.auto
            ? "marvel0.stack.monitor.auto_on"
            : "marvel0.stack.monitor.auto_off",
        )}
      </p>
      {view.monitor.plan && (
        <p>
          {t("marvel0.stack.monitor.plan")}: {sayText(view.monitor.plan, t)}
        </p>
      )}
      {view.monitor.last && (
        <p>
          {t("marvel0.stack.monitor.last")}: {sayText(view.monitor.last, t)}
        </p>
      )}
      <p className="rg-muted">
        {t("marvel0.stack.lock")}: {sayText(view.lock, t)}
      </p>
    </details>
  );
}
