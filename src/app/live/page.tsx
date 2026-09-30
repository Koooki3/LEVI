"use client";
// Live page: evaluation sessions, the FR3 arm, the annotation pipeline and the
// background service, all read-only. Data comes from `/api/levi/live/*`
// (docs/LIVE.md); nothing here starts, stops or approves anything.
import { useEffect, useMemo, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { ago } from "@/components/pool/pool-progress";
import { DatasetCard } from "@/components/live/dataset-card";
import {
  datasetFaultKind,
  detectFault,
  rankDatasets,
  type ReviewFilter,
} from "@/components/live/live-logic";
import { ServiceOffline, ServicePanel } from "@/components/live/service-panel";
import {
  FaultBanner,
  Fr3Panel,
  SessionsPanel,
} from "@/components/live/session-panels";
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
    <p className="levi-pool-hint levi-live-fresh" aria-live="off">
      {lastOk == null
        ? t("Loading…")
        : `${t("Updated")} ${ago((now - lastOk) / 1000, t)}`}
      {" · "}
      {t("refreshes every")} {Math.round(delay / 1000)} s
      {failures > 0 && (
        <span className="levi-live-bad">
          {" · "}
          {t("last request failed")} ({error}), {t("retrying")}
        </span>
      )}
      {" · "}
      {t("read-only")}
    </p>
  );
}

export default function LivePage() {
  const { t } = useLocale();
  const poll = useLivePoll();
  const { status } = poll;
  const service = status?.service ?? null;
  const sessions = poll.sessions?.sessions ?? service?.sessions ?? [];
  const fr3 = poll.sessions?.fr3 ?? service?.fr3 ?? null;
  const rows = useMemo(() => service?.datasets ?? {}, [service]);
  // No answer at all (or the core stopped answering): nothing is current.
  const coreDown = poll.failures >= (status ? 2 : 1);
  const alive = !!status?.alive && !coreDown;
  const serviceDown = !!status?.enabled && !alive;

  const fault = useMemo(() => detectFault(fr3, sessions), [fr3, sessions]);

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
  const details = useDatasetDetails(
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
    <main className="levi-workbench levi-live">
      <span className="levi-eyebrow">{t("LEVI / LIVE")}</span>
      <h1>{t("Live evaluation & annotation")}</h1>
      <p>
        {t(
          "Watch a robot evaluation while it runs: the evaluation sessions, the FR3 arm, and how far the background LEVI has got with labelling the finished episodes. Everything here is read-only. Automatic results are unreviewed and their accuracy has not been evaluated.",
        )}
      </p>
      <Freshness
        lastOk={poll.lastOk}
        delay={poll.delay}
        failures={poll.failures}
        error={poll.error}
      />
      <FaultBanner fault={fault} />

      {status?.enabled === false ? (
        <section className="levi-live-offline" role="status">
          <strong>{t("This LEVI is not the live annotation workspace")}</strong>
          <p>
            {t(
              "The live page shows the workspace that `levi live start` created. Open that service's own page (by default http://127.0.0.1:7880).",
            )}
          </p>
        </section>
      ) : (
        <>
          {(coreDown || serviceDown) && (
            <ServiceOffline
              status={status}
              coreError={coreDown ? poll.error : ""}
            />
          )}
          <div className="levi-live-grid">
            <div className="levi-live-main">
              <SessionsPanel sessions={sessions} />
              <section
                className="levi-live-section"
                aria-labelledby="live-pipeline"
              >
                <h2 id="live-pipeline">
                  {t("Annotation pipeline")}{" "}
                  <span className="levi-pool-muted">({ranked.length})</span>
                </h2>
                <p className="levi-pool-hint">
                  {t(
                    "One card per model and task folder. Finished episodes are linked into LEVI, given time segments and an automatic success or failure; nothing is written as a person's label.",
                  )}
                </p>
                {ranked.length === 0 ? (
                  <p className="levi-pool-hint">
                    {t("No dataset has been seen yet.")}
                  </p>
                ) : (
                  <div className="levi-live-cards">
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
                      />
                    ))}
                  </div>
                )}
              </section>
            </div>
            <aside className="levi-live-side">
              <Fr3Panel fr3={fr3} />
              <ServicePanel status={status} alive={alive} now={now} />
            </aside>
          </div>
        </>
      )}
    </main>
  );
}
