"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { Button, ExpandableText, SkeletonText } from "@/components/ds";
import { Note, Problem } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { useLivePoll } from "./use-live";
import { SessionCard } from "./session-panels";
import { findSession, identityOf, type SessionIdentity } from "./session-route";
import { useLiveStats } from "./use-live-stats";
import { EPISODE_PAGE, type EpisodeView } from "./stats-logic";
import {
  KeyFigures,
  EpisodesTable,
  ExportLinks,
  StatsEmpty,
} from "./stats-panel";
import { canShowStatistics, resultSessionPath } from "./result-api";
import { useResults } from "./use-results";
import { SessionResultPanel } from "./session-results";
import { sessionIsBusy } from "./live-filters";
import { sessionStatisticsMatch } from "./session-stats-source";

export function SessionDetail({
  identity,
}: {
  identity: SessionIdentity | null;
}) {
  const { t } = useLocale();
  const poll = useLivePoll();
  const sessions =
    poll.sessions?.sessions ?? poll.status?.service?.sessions ?? [];
  const session = identity ? findSession(identity, sessions) : null;
  const dataset = identity?.dataset || session?.dataset || "";
  const scope = {
    dataset,
    session: identity?.run || identity?.id || "",
    includeExcluded: false,
  };
  const [limit, setLimit] = useState(EPISODE_PAGE);
  const [view, setView] = useState<EpisodeView>("latency");
  // A legacy dataset/run bookmark can use a uniquely matched current source,
  // but never guess a root from a display name or use old statistics as permission.
  const sourceIdentity =
    identity && !identity.root && session ? identityOf(session) : identity;
  const resultRequest = useResults(
    sourceIdentity ? resultSessionPath(sourceIdentity) : null,
    poll.lastOk == null ? null : Math.floor(poll.lastOk / 30000),
  );
  const resultItem =
    resultRequest.data && "identity" in resultRequest.data
      ? resultRequest.data
      : null;
  const statsEnabled =
    !!sourceIdentity &&
    !!resultItem &&
    canShowStatistics(resultItem, { ...sourceIdentity, dataset });
  const stats = useLiveStats(
    scope,
    limit,
    poll.lastOk,
    !!session && sessionIsBusy(session),
    statsEnabled,
  );
  const statsData =
    statsEnabled &&
    !stats.loading &&
    !stats.error &&
    sessionStatisticsMatch(
      stats.data,
      scope,
      resultItem?.identity.datasets ?? [],
    )
      ? stats.data
      : null;
  const invalidStatsSource =
    !!stats.data && stats.data.enabled !== false && !statsData;
  const offline = poll.failures > 0;
  useEffect(() => {
    if (!offline || !statsEnabled) return;
    // Status may stay offline while the archived statistics service recovers.
    // One 30 s retry clock only while offline; online refreshes still follow the poll.
    const timer = setInterval(stats.refresh, 30_000);
    return () => clearInterval(timer);
  }, [offline, statsEnabled, stats.refresh]);
  const refresh = () => {
    poll.refresh();
    resultRequest.refresh();
  };
  return (
    <main className="ds-root pg-workbench pg-live pg-live-detail">
      <Link className="pg-back-link ds-focus" href="/live">
        {t("Back to live evaluation")}
      </Link>
      <h1>{t("Evaluation session details")}</h1>
      {!identity ? (
        <Problem
          title={t("Invalid session link")}
          why={t(
            "Open a session from live evaluation or its statistics table.",
          )}
        />
      ) : (
        <>
          <div className="pg-pool-muted">
            <ExpandableText
              text={
                identity.group
                  ? `${identity.group} / ${identity.task}`
                  : dataset
              }
              mono
              previewLength={56}
            />{" "}
            · <code>{identity.run || identity.id}</code>
          </div>
          {poll.failures > 0 && (
            <Note tone="warning" role="status">
              {t(
                "Live status is unavailable. Saved statistics may still be available.",
              )}{" "}
              <Button variant="ghost" size="sm" onClick={refresh}>
                {t("Refresh")}
              </Button>
            </Note>
          )}
          {session ? (
            <SessionCard
              session={
                statsData ? session : { ...session, last_episode: undefined }
              }
              open
              onToggle={() => {}}
              onChanged={refresh}
              detail
            />
          ) : !poll.lastOk && poll.failures === 0 ? (
            <SkeletonText lines={3} />
          ) : (
            <Note tone="info">
              {t(
                "This session is no longer reporting. The statistics below describe saved annotation records, not a current robot state.",
              )}
            </Note>
          )}
          <SessionResultPanel
            identity={sourceIdentity ?? identity}
            request={resultRequest}
          />
          <details className="pg-live-section pg-live-disclosure">
            <summary id="session-statistics">
              {t("Performance statistics")}
              {statsData?.summary?.episodes?.count != null
                ? ` · ${statsData.summary.episodes.count} ${t("Episodes")}`
                : ""}
            </summary>
            {!statsEnabled ? (
              <Note tone="info">
                {t(
                  "Outcome statistics are shown only after the exact source confirms that results are visible.",
                )}
              </Note>
            ) : !dataset ? (
              <Note tone="info">
                {t(
                  "No dataset is linked to this session yet. Statistics will appear after its episodes are registered.",
                )}
              </Note>
            ) : (
              <>
                {statsData && <ExportLinks scope={scope} />}
                {invalidStatsSource && (
                  <Note tone="warning" role="status">
                    {t(
                      "Statistics source could not be verified. Refresh to check this session again.",
                    )}
                    <Button size="sm" onClick={stats.refresh}>
                      {t("Refresh")}
                    </Button>
                  </Note>
                )}
                {stats.error && (
                  <Problem
                    title={t("Could not read the statistics")}
                    why={stats.error}
                    fix={
                      <Button size="sm" onClick={stats.refresh}>
                        {t("Refresh")}
                      </Button>
                    }
                  />
                )}
                {!stats.data && !stats.error && <SkeletonText lines={3} />}
                {stats.data?.enabled === false && (
                  <Note tone="info">
                    {t(
                      "Live statistics are unavailable while the live workspace is disabled.",
                    )}
                  </Note>
                )}
                {statsData && (
                  <>
                    <KeyFigures summary={statsData.summary} />
                    {(statsData.summary?.episodes?.count ?? 0) === 0 && (
                      <StatsEmpty filtered />
                    )}
                    {statsData.episodes && (
                      <EpisodesTable
                        data={statsData.episodes}
                        view={view}
                        onView={setView}
                        onMore={() => setLimit((n) => n + EPISODE_PAGE)}
                      />
                    )}
                  </>
                )}
              </>
            )}
          </details>
        </>
      )}
    </main>
  );
}
