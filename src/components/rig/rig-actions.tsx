"use client";
// The action section of the Rig console: the Policy server card and the Client
// command card, with what they share (the model list, the site view and the
// policy server's state). They are the only things on /rig that do anything:
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
import { rigApi, type RigApi } from "./deploy-api";
import {
  isFormat,
  useDeployPoll,
  useRigCatalog,
  type ReadProblem,
} from "./use-rig-actions";

export function RigActions({
  api = rigApi,
  pollMs,
}: {
  api?: RigApi;
  /** Milliseconds between reads of the policy server's state (default 3 s). */
  pollMs?: number;
}) {
  const { t } = useLocale();
  const catalog = useRigCatalog(api);
  const [logOpen, setLogOpen] = useState(false);
  const poll = useDeployPoll(api, { pollMs, logOpen });
  const site = [catalog.modelsProblem, catalog.siteProblem, poll.problem].find(
    (problem): problem is Exclude<ReadProblem, { format: true }> =>
      isSiteProblem(problem) && !isFormat(problem!),
  );
  return (
    <section
      className="rg-section"
      data-testid="rig-actions"
      aria-labelledby="rig-actions-title"
    >
      <h2 id="rig-actions-title">{t("rig.act.title")}</h2>
      <Note tone="warning">{t("rig.act.scope")}</Note>
      {site ? (
        <SiteIssue failure={site} />
      ) : !catalog.loaded ? (
        <p role="status" className="rg-muted">
          {t("rig.act.reading")}
        </p>
      ) : (
        <>
          {(catalog.modelsProblem || catalog.siteProblem) && (
            <div className="rg-unreadable">
              {catalog.modelsProblem && (
                <ReadNote
                  problem={catalog.modelsProblem}
                  formatKey="rig.act.models_format"
                  failedKey="rig.act.models_failed"
                />
              )}
              {catalog.siteProblem && (
                <ReadNote
                  problem={catalog.siteProblem}
                  formatKey="rig.act.site_format"
                  failedKey="rig.act.site_failed"
                />
              )}
              <Button size="sm" icon={RefreshCw} onClick={catalog.reload}>
                {t("rig.act.reload")}
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
