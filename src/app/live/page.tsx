"use client";
// Live page: evaluation sessions, the FR3 arm, the annotation pipeline and the
// background service. Data comes from `/api/levi/live/*` (docs/LIVE.md);
// Nothing here starts, stops or approves a robot or model. People can manage
// finished sessions, pipelines and episodes after a deletion preview.
import "@/components/pages-ui/pages.css";
import { useEffect, useMemo, useState } from "react";
import { Inbox, Radio } from "lucide-react";
import { Button, EmptyState, Skeleton } from "@/components/ds";
import { EmptyLine, Note } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import { DatasetCard } from "@/components/live/dataset-card";
import {
  datasetFaultKind,
  datasetOfSession,
  detectFault,
  liveSource,
  needsPerson,
  rankDatasets,
  type ReviewFilter,
} from "@/components/live/live-logic";
import {
  AttentionBanner,
  BlockedRunsNote,
  ServiceOffline,
  ServicePanel,
} from "@/components/live/service-panel";
import {
  FaultBanner,
  Fr3Panel,
  SessionsPanel,
} from "@/components/live/session-panels";
import {
  CommandText,
  CopyCommandButton,
  commandsIn,
} from "@/components/live/command-text";
import { disabledText } from "@/components/live/embedding";
import { LiveServiceBlock } from "@/components/live/live-service-block";
import { ResultSourcesDisclosure } from "@/components/live/result-directory";
import { LiveSummaryBar, liveSummary } from "@/components/live/live-summary";
import { useDatasetDetails, useLivePoll } from "@/components/live/use-live";
import { LiveFilterBar } from "@/components/live/filter-bar";
import {
  EMPTY_LIVE_FILTERS,
  filterLiveDatasets,
  filterLiveSessions,
  liveFilterOptions,
  sessionIsBusy,
  type LiveFilters,
} from "@/components/live/live-filters";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";

const FILTER_KEY = "levi-live-review-filter";

function loadFilter(): ReviewFilter {
  const value = readBrowserStorage("local", FILTER_KEY);
  return value === "day" || value === "all" ? value : "latest";
}

/** "Updated 3 s ago · refreshes every 2 s", counting up between polls; a
 * failed request, or one of the two that failed, is said here in words. */
function Freshness({
  lastOk,
  delay,
  failures,
  error,
  partial,
}: {
  lastOk: number | null;
  delay: number;
  failures: number;
  error: string;
  partial: "" | "status" | "sessions";
}) {
  const { t, language } = useLocale();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);
  const detail = (text: string) =>
    language === "zh" ? `（${text}）` : ` (${text})`;
  return (
    <p className="pg-pool-hint pg-live-fresh" aria-live="off">
      {lastOk == null
        ? t("Loading…")
        : `${t("Updated")} ${ago((now - lastOk) / 1000, t)}`}
      {" · "}
      {t("refreshes every {n} s").replace(
        "{n}",
        String(Math.round(delay / 1000)),
      )}
      {failures > 0 && (
        <span className="pg-live-bad">
          {" · "}
          {t("last request failed")}
          {error && detail(error)}
          {language === "zh" ? "，" : ", "}
          {t("retrying")}
        </span>
      )}
      {failures === 0 && partial && (
        <span className="pg-live-bad">
          {" · "}
          {t(
            partial === "sessions"
              ? "The session list could not be refreshed: sessions and the FR3 state come from the service status"
              : "The service status could not be refreshed: its figures are the last good ones",
          )}
          {error && detail(error)}
        </span>
      )}
    </p>
  );
}

/** Open a `<details>` a jump link points at and move focus to it, so the link
 * lands on content rather than on a closed summary. */
function openTarget(id: string) {
  const target = document.getElementById(id);
  if (!target) return;
  const details =
    target instanceof HTMLDetailsElement ? target : target.closest("details");
  if (details && !details.open) details.open = true;
  const focusable =
    details?.querySelector<HTMLElement>(":scope > summary") ?? target;
  focusable.focus({ preventScroll: true });
}

// In the page's order: the main column, then the side column.
const SECTIONS: [string, string][] = [
  ["live-sessions", "Evaluation sessions"],
  ["live-pipeline", "Annotation pipeline"],
  ["live-results-region", "Result sources and policy comparison"],
  ["live-fr3", "FR3 robot arm"],
  ["live-service-region", "Background service"],
];

export default function LivePage() {
  const { t } = useLocale();
  const poll = useLivePoll();
  const { status } = poll;
  const service = status?.service ?? null;
  // From whichever endpoint answered last (one may be failing).
  const source = useMemo(
    () =>
      liveSource({
        status: poll.status,
        sessions: poll.sessions,
        statusOkAt: poll.statusOkAt,
        sessionsOkAt: poll.sessionsOkAt,
      }),
    [poll.status, poll.sessions, poll.statusOkAt, poll.sessionsOkAt],
  );
  const { sessions, fr3 } = source;
  const rows = useMemo(() => service?.datasets ?? {}, [service]);
  const loaded = poll.lastOk != null;
  // No answer at all (or the core stopped answering): nothing is current.
  const coreDown = poll.failures >= (status ? 2 : 1);
  const alive = !!status?.alive && !coreDown;
  const serviceDown = !!status?.enabled && !alive;

  const fault = useMemo(() => detectFault(fr3, sessions), [fr3, sessions]);
  const need = useMemo(
    () => needsPerson(service, rows, (poll.lastOk ?? Date.now()) / 1000),
    [service, rows, poll.lastOk],
  );
  const sessionActive = sessions.some(sessionIsBusy);
  const monitorSilent =
    loaded && sessionActive && fr3?.state !== "ok" && fr3?.state !== "red";

  const [open, setOpen] = useState<Set<string>>(new Set());
  const [listFilters, setListFilters] = useState<LiveFilters>({
    ...EMPTY_LIVE_FILTERS,
  });
  const [filter, setFilter] = useState<ReviewFilter>("latest");
  useEffect(() => setFilter(loadFilter()), []);
  // A jump link (or a shared URL with its hash) opens its section.
  useEffect(() => {
    const onHash = () => {
      const id = window.location.hash.slice(1);
      if (SECTIONS.some(([section]) => section === id)) openTarget(id);
    };
    onHash();
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [loaded]);
  const ranked = useMemo(
    () => rankDatasets(rows, sessions, fault.redLight),
    [rows, sessions, fault.redLight],
  );
  const filterOptions = useMemo(
    () => liveFilterOptions(sessions, rows, listFilters),
    [sessions, rows, listFilters],
  );
  const shownSessions = useMemo(
    () => filterLiveSessions(sessions, listFilters),
    [sessions, listFilters],
  );
  const shownDatasets = useMemo(
    () => filterLiveDatasets(ranked, rows, sessions, listFilters),
    [ranked, rows, sessions, listFilters],
  );
  // Forget the open state of pipelines that are gone (deleted), so one
  // created again under the same name starts closed.
  useEffect(() => {
    setOpen((prev) => {
      const kept = [...prev].filter((name) => name in rows);
      return kept.length === prev.size ? prev : new Set(kept);
    });
  }, [rows]);
  const wanted = useMemo(
    () => shownDatasets.filter((name) => open.has(name)),
    [shownDatasets, open],
  );
  const { details, refresh } = useDatasetDetails(
    wanted,
    rows,
    open,
    status?.enabled === true,
  );

  const toggle = (name: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  const now = poll.lastOk ?? Date.now();
  const disabled = status?.enabled === false && !coreDown;

  return (
    <main className="ds-root ds-page pg-workbench pg-live">
      <h1>{t("Live evaluation & annotation")}</h1>
      {status?.enabled && status.workspace_name && (
        <p className="pg-pool-muted">
          {t("Live workspace")}: <code>{status.workspace_name}</code>
        </p>
      )}
      <Freshness
        lastOk={poll.lastOk}
        delay={poll.delay}
        failures={poll.failures}
        error={poll.error}
        partial={poll.partial}
      />
      {loaded && !disabled && (
        <LiveSummaryBar
          summary={liveSummary(sessions, rows, service)}
          fr3={coreDown ? null : (fr3?.state ?? "missing")}
          stale={!alive}
        />
      )}
      <FaultBanner fault={fault} />
      {monitorSilent && !coreDown && (
        <Note tone="warning" role="status">
          {t(
            "An evaluation session is active but the FR3 health monitor is not reporting, so this page cannot show a red light. Watch the robot itself, or start the monitor in the ROS terminal.",
          )}
        </Note>
      )}
      {alive && <AttentionBanner need={need} />}
      {alive && <BlockedRunsNote status={status} />}
      {(coreDown || serviceDown) && (
        <ServiceOffline
          status={status}
          coreError={coreDown ? poll.error : ""}
        />
      )}
      {status?.enabled && status.pool_memory_full && (
        <Note tone="warning" role="status">
          <CommandText
            text={t(
              "The training pool already remembers 20 live workspaces and does not remember this one: episodes removed here are kept out of the pool only while this page shows it. Make room with `levi pool live-workspaces forget <path>`.",
            )}
          />
        </Note>
      )}
      <details className="pg-live-intro">
        <summary>{t("About live evaluation")}</summary>
        <Note tone="info">
          {t(
            "Watch evaluation sessions, the FR3 arm and background annotation. Expand a row for details. Finished sessions and pipelines can be deleted after a preview; manage individual source files in the local dataset viewer. Automatic results are unreviewed and their accuracy has not been evaluated.",
          )}{" "}
          {t(
            "One row per model and task folder. Expand it to see annotation progress and episodes. Finished episodes are registered for viewing independently of model annotation.",
          )}{" "}
          {t(
            "Dates filter the latest recorded activity. Global faults and service status remain visible.",
          )}
        </Note>
      </details>

      {!loaded ? (
        !coreDown && (
          <div className="pg-live-loading-page" aria-busy="true">
            <span className="sr-only">{t("Loading…")}</span>
            <Skeleton width="100%" height={56} />
            <Skeleton width="100%" height={160} />
          </div>
        )
      ) : status?.enabled === false ? (
        disabled && (
          <>
            <div role="status">
              <EmptyState
                icon={Radio}
                title={t(disabledText(status.reason).title)}
                description={
                  <CommandText text={t(disabledText(status.reason).body)} />
                }
                action={
                  commandsIn(disabledText(status.reason).body)[0] ? (
                    <CopyCommandButton
                      command={commandsIn(disabledText(status.reason).body)[0]}
                    />
                  ) : undefined
                }
              />
              <Button size="sm" variant="ghost" onClick={poll.refresh}>
                {t("Check again now")}
              </Button>
            </div>
            <LiveServiceBlock />
          </>
        )
      ) : (
        <>
          <nav className="pg-live-jump" aria-label={t("On this page")}>
            {SECTIONS.map(([id, label]) => (
              <a key={id} href={`#${id}`} onClick={() => openTarget(id)}>
                {t(label)}
              </a>
            ))}
          </nav>
          <LiveFilterBar
            filters={listFilters}
            options={filterOptions}
            onChange={setListFilters}
          />
          <div className="pg-live-layout">
            <div className="pg-live-column pg-live-column--main">
              <div className="pg-live-area pg-live-area--sessions">
                <SessionsPanel
                  sessions={shownSessions}
                  total={sessions.length}
                  filterKey={JSON.stringify(listFilters)}
                  resultTick={Math.floor(now / 30000)}
                  onChanged={poll.refresh}
                />
              </div>
              <section
                className="pg-live-section pg-live-area pg-live-area--pipeline"
                aria-labelledby="live-pipeline"
              >
                <h2 id="live-pipeline">
                  {t("Annotation pipeline")}{" "}
                  <span className="pg-pool-muted">
                    ({shownDatasets.length} / {ranked.length})
                  </span>
                </h2>
                {shownDatasets.length === 0 ? (
                  <EmptyLine icon={Inbox}>
                    {t(
                      ranked.length > 0
                        ? "No annotation pipelines match these filters."
                        : "No dataset has been seen yet.",
                    )}
                  </EmptyLine>
                ) : (
                  <div className="pg-live-cards">
                    {shownDatasets.map((name) => (
                      <DatasetCard
                        key={name}
                        name={name}
                        row={rows[name]}
                        entry={details[name]}
                        fault={datasetFaultKind(
                          name,
                          rows[name],
                          sessions,
                          fault.redLight,
                        )}
                        open={open.has(name)}
                        onToggle={() => toggle(name)}
                        workerPhase={
                          alive && service?.worker?.dataset === name
                            ? (service.worker.phase ?? null)
                            : null
                        }
                        alive={alive}
                        filter={filter}
                        onFilter={(value) => {
                          setFilter(value);
                          writeBrowserStorage("local", FILTER_KEY, value);
                        }}
                        nowSeconds={now / 1000}
                        onChanged={() => refresh(name)}
                        onDeleted={poll.refresh}
                        busy={sessions.some(
                          (session) =>
                            datasetOfSession(session) === name &&
                            sessionIsBusy(session),
                        )}
                      />
                    ))}
                  </div>
                )}
              </section>
              <div className="pg-live-area pg-live-area--results">
                <ResultSourcesDisclosure />
              </div>
            </div>
            <div className="pg-live-column pg-live-column--side">
              <div className="pg-live-area pg-live-area--fr3">
                <Fr3Panel fr3={fr3} stale={coreDown} />
              </div>
              <details
                className="pg-live-section pg-live-disclosure pg-live-area pg-live-area--service"
                id="live-service-region"
              >
                <summary>{t("Background service")}</summary>
                <LiveServiceBlock />
                <ServicePanel
                  status={status}
                  alive={alive}
                  coreDown={coreDown}
                  now={now}
                />
              </details>
            </div>
          </div>
        </>
      )}
    </main>
  );
}
