"use client";
// Steps 2, 3 and 4 of the marvel0: the Policy server card, the Client command
// card, and the Run card of the hosted client. Each of the first two has the
// status card of what it is about (passed in by the page) beside it, the
// control first; what they share is the model list, the site view, the policy
// server's state and the one selected model. The first starts and stops the
// one policy server unit this page created, the second writes the demo
// client's command and can start it, the third holds and drives the client
// the page hosts.
import { useEffect, useState, type ReactNode } from "react";
import { useLocale } from "@/components/levi-locale";
import { Button } from "@/components/ds";
import { RefreshCw } from "lucide-react";
import { ReadNote, SiteIssue, isSiteProblem } from "./action-parts";
import { CommandPanel } from "./command-panel";
import { DeployPanel, MODEL_CONTROL } from "./deploy-panel";
import { marvel0Api, type Marvel0Api } from "./deploy-api";
import type { DeployStatus } from "./deploy-logic";
import { StepHeading } from "./marvel0-card";
import { RunPanel } from "./run-panel";
import { effectiveSelection, useChoice } from "./selection";
import type { RunActions } from "./use-run-actions";
import type { RunPoll } from "./use-run-poll";
import {
  isFormat,
  useDeployPoll,
  useMarvel0Catalog,
  type ReadProblem,
} from "./use-marvel0-actions";

export function Marvel0Actions({
  api = marvel0Api,
  pollMs,
  policyStatus,
  clientStatus,
  run,
  runActions,
  onDeploy,
}: {
  api?: Marvel0Api;
  /** Milliseconds between reads of the policy server's state (default 3 s). */
  pollMs?: number;
  /** The status card shown first in step 2 (the policy server's). */
  policyStatus?: ReactNode;
  /** The status card shown first in step 3 (the demo client's). */
  clientStatus?: ReactNode;
  /** The hosted client's state and what its buttons do (the page reads and
   * owns them: the chain at the top follows the same state). Without them
   * the step shows the command card alone. */
  run?: RunPoll;
  runActions?: RunActions;
  /** Told the policy server's state whenever a read changes it; null while
   * the last read is not current. The chain at the top follows it. */
  onDeploy?: (status: DeployStatus | null) => void;
}) {
  const { t } = useLocale();
  const catalog = useMarvel0Catalog(api);
  const [logOpen, setLogOpen] = useState(false);
  const poll = useDeployPoll(api, { pollMs, logOpen });
  const choice = useChoice();
  const selection = effectiveSelection(
    catalog.models,
    choice.choice,
    poll.status,
  );
  const deployNow = poll.current ? poll.status : null;
  useEffect(() => {
    onDeploy?.(deployNow);
  }, [onDeploy, deployNow]);
  // "Use another model" moves the keyboard to the policy server's model control.
  const useAnother = () => {
    const control = document.querySelector<HTMLElement>(`[${MODEL_CONTROL}]`);
    control?.scrollIntoView?.({ block: "center" });
    control?.focus?.({ preventScroll: true });
  };
  const site = [catalog.modelsProblem, catalog.siteProblem, poll.problem].find(
    (problem): problem is Exclude<ReadProblem, { format: true }> =>
      isSiteProblem(problem) && !isFormat(problem!),
  );
  const ready = !site && catalog.loaded;
  return (
    <div className="rg-steps" data-testid="marvel0-actions">
      <section
        className="rg-section rg-step"
        aria-labelledby="marvel0-step-policy"
      >
        <StepHeading n={2} id="marvel0-step-policy">
          {t("marvel0.step.policy")}
        </StepHeading>
        {site ? (
          <SiteIssue failure={site} />
        ) : !catalog.loaded ? (
          <p role="status" className="rg-muted">
            {t("marvel0.act.reading")}
          </p>
        ) : (
          (catalog.modelsProblem || catalog.siteProblem) && (
            <div className="rg-unreadable">
              {catalog.modelsProblem && (
                <ReadNote
                  problem={catalog.modelsProblem}
                  formatKey="marvel0.act.models_format"
                  failedKey="marvel0.act.models_failed"
                />
              )}
              {catalog.siteProblem && (
                <ReadNote
                  problem={catalog.siteProblem}
                  formatKey="marvel0.act.site_format"
                  failedKey="marvel0.act.site_failed"
                />
              )}
              <Button size="sm" icon={RefreshCw} onClick={catalog.reload}>
                {t("marvel0.act.reload")}
              </Button>
            </div>
          )
        )}
        <div className={`rg-step__grid${ready ? "" : " rg-step__grid--one"}`}>
          {ready && (
            <DeployPanel
              api={api}
              catalog={catalog}
              poll={poll}
              onLogOpen={setLogOpen}
              choice={choice}
            />
          )}
          {policyStatus}
        </div>
      </section>
      <section
        className="rg-section rg-step"
        aria-labelledby="marvel0-step-client"
      >
        <StepHeading n={3} id="marvel0-step-client">
          {t("marvel0.step.client")}
        </StepHeading>
        {site && <p className="rg-muted">{t("marvel0.step.see_policy")}</p>}
        <div className={`rg-step__grid${ready ? "" : " rg-step__grid--one"}`}>
          {ready && (
            <CommandPanel
              api={api}
              catalog={catalog}
              status={poll.current ? poll.status : null}
              selection={selection}
              onUseAnother={useAnother}
              run={run?.value ?? null}
              runActions={runActions}
            />
          )}
          {clientStatus}
        </div>
      </section>
      {run && runActions && (
        <section
          className="rg-section rg-step"
          aria-labelledby="marvel0-step-run"
          data-testid="marvel0-step-run"
        >
          <StepHeading n={4} id="marvel0-step-run">
            {t("marvel0.step.run")}
          </StepHeading>
          <RunPanel poll={run} actions={runActions} />
        </section>
      )}
    </div>
  );
}
