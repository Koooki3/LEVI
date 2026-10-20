"use client";
// The action section of the marvel0: the Policy server card and the Client
// command card, with what they share (the model list, the site view and the
// policy server's state). They are the only things on /marvel0 that do anything:
// the first starts and stops the one policy server unit this page created, the
// second writes the demo client's command for a person to copy. Everything
// else on the page is a read-only status card.
import { useState } from "react";
import { Note } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { Button } from "@/components/ds";
import { RefreshCw } from "lucide-react";
import { ReadNote, SiteIssue, isSiteProblem } from "./action-parts";
import { CommandPanel } from "./command-panel";
import { DeployPanel } from "./deploy-panel";
import { marvel0Api, type Marvel0Api } from "./deploy-api";
import {
  isFormat,
  useDeployPoll,
  useMarvel0Catalog,
  type ReadProblem,
} from "./use-marvel0-actions";

export function Marvel0Actions({
  api = marvel0Api,
  pollMs,
}: {
  api?: Marvel0Api;
  /** Milliseconds between reads of the policy server's state (default 3 s). */
  pollMs?: number;
}) {
  const { t } = useLocale();
  const catalog = useMarvel0Catalog(api);
  const [logOpen, setLogOpen] = useState(false);
  const poll = useDeployPoll(api, { pollMs, logOpen });
  const site = [catalog.modelsProblem, catalog.siteProblem, poll.problem].find(
    (problem): problem is Exclude<ReadProblem, { format: true }> =>
      isSiteProblem(problem) && !isFormat(problem!),
  );
  return (
    <section
      className="rg-section"
      data-testid="marvel0-actions"
      aria-labelledby="marvel0-actions-title"
    >
      <h2 id="marvel0-actions-title">{t("marvel0.act.title")}</h2>
      <Note tone="warning">{t("marvel0.act.scope")}</Note>
      {site ? (
        <SiteIssue failure={site} />
      ) : !catalog.loaded ? (
        <p role="status" className="rg-muted">
          {t("marvel0.act.reading")}
        </p>
      ) : (
        <>
          {(catalog.modelsProblem || catalog.siteProblem) && (
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
          )}
          <div className="rg-actions">
            <DeployPanel
              api={api}
              catalog={catalog}
              poll={poll}
              onLogOpen={setLogOpen}
            />
            <CommandPanel
              api={api}
              catalog={catalog}
              status={poll.current ? poll.status : null}
              deployedModel={poll.status?.deploy?.modelId ?? null}
            />
          </div>
        </>
      )}
    </section>
  );
}
