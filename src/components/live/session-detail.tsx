"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { Button, SkeletonText } from "@/components/ds";
import { Note, Problem } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { useLivePoll } from "./use-live";
import { SessionCard } from "./session-panels";
import { findSession, type SessionIdentity } from "./session-route";
import { useLiveStats } from "./use-live-stats";
import { EPISODE_PAGE, type EpisodeView } from "./stats-logic";
import {
  KeyFigures,
  EpisodesTable,
  ExportLinks,
  StatsEmpty,
} from "./stats-panel";
import { sessionIsBusy } from "./live-filters";

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
  const statsEnabled = !!identity && !!dataset;
  const stats = useLiveStats(
    scope,
    limit,
    poll.lastOk,
    !!session && sessionIsBusy(session),
    statsEnabled,
  );
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
    stats.refresh();
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
          <p className="pg-pool-muted">
            <code>
              {identity.group
                ? `${identity.group} / ${identity.task}`
                : dataset}
            </code>{" "}
            · <code>{identity.run || identity.id}</code>
          </p>
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
              session={session}
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
          <section
            className="pg-live-section"
            aria-labelledby="session-statistics"
          >
            <h2 id="session-statistics">{t("Statistics")}</h2>
            {!dataset ? (
              <Note tone="info">
                {t(
                  "No dataset is linked to this session yet. Statistics will appear after its episodes are registered.",
                )}
              </Note>
            ) : (
              <>
                <ExportLinks scope={scope} />
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
                {stats.data && stats.data.enabled !== false && (
                  <>
                    <KeyFigures summary={stats.data.summary} />
                    {(stats.data.summary?.episodes?.count ?? 0) === 0 && (
                      <StatsEmpty filtered />
                    )}
                    {stats.data.episodes && (
                      <EpisodesTable
                        data={stats.data.episodes}
                        view={view}
                        onView={setView}
                        onMore={() => setLimit((n) => n + EPISODE_PAGE)}
                      />
                    )}
                  </>
                )}
              </>
            )}
          </section>
        </>
      )}
    </main>
  );
}
