"use client";
// The "Statistics" section of the live page: key figures, the evaluation
// sessions, one row per episode, and downloads. Read-only; the data is
// `GET /api/levi/live/stats` (docs/LIVE.md, "Statistics and reports").
import { useEffect, useRef, useState } from "react";
import { useLocale } from "@/components/levi-locale";
import { clock } from "./live-logic";
import {
  columns,
  count,
  EPISODE_PAGE,
  EXPORT_FORMATS,
  exportHref,
  factor,
  fill,
  kpis,
  percent,
  seconds,
  sessionChoices,
  type EpisodeView,
  type StatsResponse,
  type StatsScope,
  type StatsSession,
} from "./stats-logic";
import { useLiveStats } from "./use-live-stats";

const FORMAT_LABEL = { md: "Markdown", json: "JSON", csv: "CSV" } as const;

export function KeyFigures({ summary }: { summary: StatsResponse["summary"] }) {
  const { t } = useLocale();
  return (
    <div className="levi-live-kpis" role="list">
      {kpis(summary).map((k) => (
        <div key={k.id} className="levi-live-kpi" role="listitem">
          <span>{t(k.label)}</span>
          <strong>{k.value}</strong>
          <small>{fill(t(k.hint), k.hintArgs)}</small>
        </div>
      ))}
    </div>
  );
}

export function SessionsTable({ rows }: { rows: StatsSession[] }) {
  const { t } = useLocale();
  if (rows.length === 0) return null;
  return (
    <>
      <h3>{t("Evaluation sessions")}</h3>
      <div className="levi-pool-table-wrap">
        <table className="levi-table levi-pool-table levi-live-stats-table">
          <thead>
            <tr>
              <th>{t("Session")}</th>
              <th className="num">{t("Episodes")}</th>
              <th className="num">{t("Median delay")}</th>
              <th className="num">{t("Slowest 10% (p90)")}</th>
              <th className="num">{t("Tokens")}</th>
              <th className="num">{t("Real-time factor")}</th>
              <th className="num">{t("Labelled during the session")}</th>
              <th className="num">{t("Gate wait")}</th>
              <th>{t("Last label")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.dataset}/${r.session}`}>
                <td>
                  <code title={r.dataset ?? ""}>{r.session || "—"}</code>
                </td>
                <td className="num">{count(r.episodes)}</td>
                <td className="num">{seconds(r.to_verdict_median_s)}</td>
                <td className="num">{seconds(r.to_verdict_p90_s)}</td>
                <td className="num">{count(r.tokens)}</td>
                <td className="num">{factor(r.realtime_factor)}</td>
                <td className="num">{percent(r.in_session_ratio)}</td>
                <td className="num">{seconds(r.closed_wait_s)}</td>
                <td>{clock(r.last_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function EpisodesTable({
  data,
  view,
  onView,
  onMore,
}: {
  data: NonNullable<StatsResponse["episodes"]>;
  view: EpisodeView;
  onView: (view: EpisodeView) => void;
  onMore: () => void;
}) {
  const { t } = useLocale();
  const rows = data.rows ?? [];
  const cols = columns(view);
  return (
    <>
      <h3>
        {t("Per episode")}{" "}
        <span className="levi-pool-muted">
          ({rows.length} / {count(data.total)})
        </span>
      </h3>
      <div className="levi-live-chips" role="group" aria-label={t("Columns")}>
        {(["latency", "cost"] as const).map((v) => (
          <button
            key={v}
            type="button"
            className={`levi-live-toggle${view === v ? " on" : ""}`}
            aria-pressed={view === v}
            onClick={() => onView(v)}
          >
            {t(v === "latency" ? "Delays" : "Model cost")}
          </button>
        ))}
      </div>
      <div className="levi-pool-table-wrap">
        <table className="levi-table levi-pool-table levi-live-stats-table">
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.key} className={c.numeric ? "num" : undefined}>
                  {t(c.label)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.dataset}/${r.demo}`}>
                {cols.map((c) => (
                  <td key={c.key} className={c.numeric ? "num" : undefined}>
                    {c.numeric ? c.cell(r) : t(c.cell(r))}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length < (data.total ?? 0) && (
        <button type="button" className="levi-live-more" onClick={onMore}>
          {t("Show more")}
        </button>
      )}
    </>
  );
}

export function ExportLinks({ scope }: { scope: StatsScope }) {
  const { t, language } = useLocale();
  return (
    <p className="levi-live-export">
      {t("Download")}:{" "}
      {EXPORT_FORMATS.map((f, i) => (
        <span key={f}>
          {i > 0 && " · "}
          <a href={exportHref(f, scope, language)} download>
            {FORMAT_LABEL[f]}
          </a>
        </span>
      ))}
    </p>
  );
}

export function StatsView({
  data,
  scope,
  onScope,
  view,
  onView,
  onMore,
  choices,
}: {
  data: StatsResponse;
  scope: StatsScope;
  onScope: (scope: StatsScope) => void;
  view: EpisodeView;
  onView: (view: EpisodeView) => void;
  onMore: () => void;
  choices: string[];
}) {
  const { t } = useLocale();
  const datasets = data.datasets ?? [];
  return (
    <>
      <div className="levi-live-scope">
        <label>
          {t("Dataset")}
          <select
            value={scope.dataset}
            onChange={(e) => onScope({ dataset: e.target.value, session: "" })}
          >
            <option value="">{t("All datasets")}</option>
            {datasets.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("Session")}
          <select
            value={scope.session}
            onChange={(e) => onScope({ ...scope, session: e.target.value })}
          >
            <option value="">{t("All sessions")}</option>
            {choices.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <ExportLinks scope={scope} />
      </div>
      <KeyFigures summary={data.summary} />
      <SessionsTable rows={data.sessions ?? []} />
      {data.episodes && (
        <EpisodesTable
          data={data.episodes}
          view={view}
          onView={onView}
          onMore={onMore}
        />
      )}
      <p className="levi-pool-hint">
        {t(
          "Figures count the newest record of each episode; a dash means the figure was not measured. Definitions: docs/LIVE.md, Statistics and reports.",
        )}
      </p>
    </>
  );
}

/** The empty state: no episode has been labelled in this scope yet. */
export function StatsEmpty({ filtered }: { filtered: boolean }) {
  const { t } = useLocale();
  return (
    <p className="levi-pool-hint" role="status">
      {filtered
        ? t("No labelled episode matches this scope.")
        : t(
            "No statistics yet. A record is written for every episode the background service labels; the first appears when a batch ends. Records of episodes labelled earlier can be added with `levi live stats backfill`.",
          )}
    </p>
  );
}

export function StatsPanel({
  tick,
  evaluating,
  enabled,
}: {
  /** Epoch ms of the page's last successful poll: a new value means "ask". */
  tick: number | null;
  evaluating: boolean;
  enabled: boolean;
}) {
  const { t } = useLocale();
  const [scope, setScope] = useState<StatsScope>({ dataset: "", session: "" });
  const [limit, setLimit] = useState(EPISODE_PAGE);
  const [view, setView] = useState<EpisodeView>("latency");
  const { data, error } = useLiveStats(scope, limit, tick, evaluating, enabled);
  // The session choices come from an answer that is not narrowed to one
  // session, so choosing one does not empty the list.
  const known = useRef<StatsSession[]>([]);
  useEffect(() => {
    if (data && !data.scope?.session) known.current = data.sessions ?? [];
  }, [data]);
  const choices = sessionChoices(
    known.current.length ? known.current : (data?.sessions ?? []),
    scope.dataset,
    scope.session,
  );
  const empty = !!data && (data.summary?.episodes?.count ?? 0) === 0;
  const filtered = !!scope.dataset || !!scope.session;
  return (
    <section
      className="levi-live-section levi-live-slot o5"
      aria-labelledby="live-statistics"
    >
      <h2 id="live-statistics">{t("Statistics")}</h2>
      <p className="levi-pool-hint">
        {t(
          "How long labelling takes after an episode ends, what the model costs, and how often the GPU gate got in the way. Measured by the service for every episode it labels.",
        )}
      </p>
      {error && !data && (
        <p className="levi-live-bad" role="alert">
          {t("Could not read the statistics")} ({error})
        </p>
      )}
      {!data && !error && <p className="levi-pool-hint">{t("Loading…")}</p>}
      {data && data.enabled === false && null}
      {data && data.enabled !== false && (
        <>
          {empty && !filtered ? (
            <StatsEmpty filtered={false} />
          ) : (
            <StatsView
              data={data}
              scope={scope}
              onScope={(next) => {
                setScope(next);
                setLimit(EPISODE_PAGE);
              }}
              view={view}
              onView={setView}
              onMore={() => setLimit((n) => n + EPISODE_PAGE)}
              choices={choices}
            />
          )}
          {empty && filtered && <StatsEmpty filtered />}
          {error && (
            <p className="levi-live-bad">
              {t("last request failed")} ({error})
            </p>
          )}
        </>
      )}
    </section>
  );
}
