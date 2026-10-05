"use client";
// Live page: evaluation sessions, the FR3 arm, the annotation pipeline and the
// background service. Data comes from `/api/levi/live/*` (docs/LIVE.md);
// nothing here starts, stops or approves anything. The one thing a person can
// change is to remove an episode from a dataset and restore it (a soft
// delete: no file is deleted).
import "@/components/pages-ui/pages.css";
import { useEffect, useMemo, useState } from "react";
import { Inbox, Radio } from "lucide-react";
import { EmptyState } from "@/components/ds";
import { EmptyLine, Note } from "@/components/pages-ui/feedback";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import { DatasetCard } from "@/components/live/dataset-card";
import {
  datasetFaultKind,
  detectFault,
  isEvaluating,
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
import { StatsPanel } from "@/components/live/stats-panel";
import { LiveSummaryBar, liveSummary } from "@/components/live/live-summary";
import { useDatasetDetails, useLivePoll } from "@/components/live/use-live";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";

const FILTER_KEY = "levi-live-review-filter";
/** Cards whose details load without being opened. */
const AUTO_DETAILS = 6;

function loadFilter(): ReviewFilter {
  const value = readBrowserStorage("local", FILTER_KEY);
  return value === "day" || value === "all" ? value : "latest";
}

/** "Updated 3 s ago · refreshes every 2 s", counting up between polls. */
function Freshness({
  lastOk,
  delay,
  failures,
  error,
}: {
  lastOk: number | null;
  delay: number;
  failures: number;
  error: string;
}) {
  const { t } = useLocale();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setNow(Date.now());
    }, 1000);
    return () => clearInterval(timer);
  }, []);
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
          {t("last request failed")} ({error}), {t("retrying")}
        </span>
      )}
      {" · "}
      {t("read-only except removing episodes")}
    </p>
  );
}

export default function LivePage() {
  const { t } = useLocale();
  const poll = useLivePoll();
  const { status } = poll;
  const service = status?.service ?? null;
  const sessionList = poll.sessions?.sessions ?? service?.sessions;
  const sessions = useMemo(() => sessionList ?? [], [sessionList]);
  const fr3 = poll.sessions?.fr3 ?? service?.fr3 ?? null;
  const rows = useMemo(() => service?.datasets ?? {}, [service]);
  // No answer at all (or the core stopped answering): nothing is current.
  const coreDown = poll.failures >= (status ? 2 : 1);
  const alive = !!status?.alive && !coreDown;
  const serviceDown = !!status?.enabled && !alive;

  const fault = useMemo(() => detectFault(fr3, sessions), [fr3, sessions]);
  const need = useMemo(
    () => needsPerson(service, rows, (poll.lastOk ?? Date.now()) / 1000),
    [service, rows, poll.lastOk],
  );

  const [open, setOpen] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<ReviewFilter>("latest");
  useEffect(() => setFilter(loadFilter()), []);
  const ranked = useMemo(
    () => rankDatasets(rows, sessions, fault.redLight),
    [rows, sessions, fault.redLight],
  );
  const wanted = useMemo(
    () => [
      ...ranked.slice(0, AUTO_DETAILS),
      ...ranked.slice(AUTO_DETAILS).filter((n) => open.has(n)),
    ],
    [ranked, open],
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

  return (
    <main className="ds-root pg-workbench pg-live">
      <h1>{t("Live evaluation & annotation")}</h1>
      {status?.enabled && status.workspace_name && (
        <p className="pg-pool-muted">
          {t("Live workspace")}: <code>{status.workspace_name}</code>
        </p>
      )}
      {status?.enabled && status.pool_memory_full && (
        <Note tone="warning" role="status">
          {t(
            "The training pool already remembers 20 live workspaces and does not remember this one: episodes removed here are kept out of the pool only while this page shows it. Make room with `levi pool live-workspaces forget <path>`.",
          )}
        </Note>
      )}
      <Note tone="info">
        {t(
          "Watch a robot evaluation while it runs: the evaluation sessions, the FR3 arm, and how far the background LEVI has got with labelling the finished episodes. Nothing here starts, stops or approves anything; the only change you can make is to remove an episode from a dataset (restorable, nothing is deleted). Automatic results are unreviewed and their accuracy has not been evaluated.",
        )}
      </Note>
      {status?.enabled !== false && (
        <LiveSummaryBar summary={liveSummary(sessions, rows, service)} />
      )}
      <Freshness
        lastOk={poll.lastOk}
        delay={poll.delay}
        failures={poll.failures}
        error={poll.error}
      />
      <FaultBanner fault={fault} />
      {alive && <AttentionBanner need={need} />}
      {alive && <BlockedRunsNote status={status} />}

      {status?.enabled === false ? (
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
        </div>
      ) : (
        <>
          {(coreDown || serviceDown) && (
            <ServiceOffline
              status={status}
              coreError={coreDown ? poll.error : ""}
            />
          )}
          <div className="pg-live-grid">
            <div className="pg-live-main">
              <div className="pg-live-slot o1">
                <SessionsPanel sessions={sessions} />
              </div>
              <section
                className="pg-live-section pg-live-slot o3"
                aria-labelledby="live-pipeline"
              >
                <h2 id="live-pipeline">
                  {t("Annotation pipeline")}{" "}
                  <span className="pg-pool-muted">({ranked.length})</span>
                </h2>
                <p className="pg-pool-hint">
                  {t(
                    "One card per model and task folder. Finished episodes are linked into LEVI, given time segments and an automatic success or failure; nothing is written as a person's label.",
                  )}
                </p>
                {ranked.length === 0 ? (
                  <EmptyLine icon={Inbox}>
                    {t("No dataset has been seen yet.")}
                  </EmptyLine>
                ) : (
                  <div className="pg-live-cards">
                    {ranked.map((name) => (
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
                          service?.worker?.dataset === name
                            ? (service.worker.phase ?? null)
                            : null
                        }
                        filter={filter}
                        onFilter={(value) => {
                          setFilter(value);
                          writeBrowserStorage("local", FILTER_KEY, value);
                        }}
                        nowSeconds={now / 1000}
                        onChanged={() => refresh(name)}
                      />
                    ))}
                  </div>
                )}
              </section>
              <StatsPanel
                tick={poll.lastOk}
                evaluating={isEvaluating(sessions, service)}
                enabled={status?.enabled === true}
              />
            </div>
            <aside className="pg-live-side">
              <div className="pg-live-slot o2">
                <Fr3Panel fr3={fr3} />
              </div>
              <div className="pg-live-slot o4">
                <ServicePanel status={status} alive={alive} now={now} />
              </div>
            </aside>
          </div>
        </>
      )}
    </main>
  );
}
