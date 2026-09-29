"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import { SelectionFields } from "./selection-fields";
import {
  METHOD_LABELS,
  PICK_REASONS,
  SELECTION_NOTES,
  STRATEGY_LABELS,
  type Mix,
  type PickedEpisodes,
  type TaskEntry,
  type TaskReport,
} from "./types";

const CATEGORY_NAMES: Record<string, string> = {
  human: "Human",
  rollout: "Rollout",
  levi: "LEVI",
  external: "External",
  archive: "Archive",
};

/** "Picked N of M · successes a · failures b", the edit button and the
 * inline editor of one task of the composition. */
export function TaskPick({
  entry,
  report,
  onChange,
}: {
  entry: TaskEntry;
  report: TaskReport | undefined;
  onChange: (patch: Partial<TaskEntry>) => void;
}) {
  const { t } = useLocale();
  const [editing, setEditing] = useState(false);
  const short = report && (report.shortfall > 0 || report.notes.length > 0);
  return (
    <div className="levi-pool-pick">
      <div className="levi-pool-pick-line">
        {report ? (
          <span className="tabular">
            {t("Picked")} <strong>{report.selected.toLocaleString()}</strong>{" "}
            {t("out of")} {report.available.toLocaleString()} · {t("successes")}{" "}
            {report.selected_successes.toLocaleString()} · {t("failures")}{" "}
            {report.selected_failures.toLocaleString()}
            {report.selected_unknown > 0
              ? ` · ${t("without outcome")} ${report.selected_unknown.toLocaleString()}`
              : ""}
          </span>
        ) : (
          <span className="levi-pool-muted">…</span>
        )}
        <span className="levi-pool-badge" title={t("How to pick")}>
          {t(STRATEGY_LABELS[entry.strategy])}
        </span>
        <button
          type="button"
          className="levi-pool-link"
          aria-expanded={editing}
          onClick={() => setEditing(!editing)}
        >
          {editing ? t("Done") : t("Edit")}
        </button>
      </div>
      {short && report && (
        <ul className="levi-pool-notes">
          {report.shortfall > 0 && (
            <li className="levi-pool-warn">
              {t("Requested")} {report.requested?.toLocaleString()}, {t("only")}{" "}
              {report.selected.toLocaleString()} {t("available")}
            </li>
          )}
          {report.notes
            .filter((n) => n !== "fewer_available")
            .map((n) => (
              <li key={n} className="levi-pool-warn">
                {t(SELECTION_NOTES[n] || n)}
                {n === "success_short" && report.shortfall_successes > 0
                  ? ` (−${report.shortfall_successes})`
                  : n === "failure_short" && report.shortfall_failures > 0
                    ? ` (−${report.shortfall_failures})`
                    : ""}
              </li>
            ))}
        </ul>
      )}
      {editing && report && (
        <SelectionFields
          available={Math.max(1, report.available)}
          successes={report.successes}
          failures={report.failures}
          unknown={report.unknown}
          value={entry}
          onChange={onChange}
        />
      )}
    </div>
  );
}

/** The episodes a task's selection picked, with quality score and reasons.
 * Fetched when opened, and again when the recipe changes. */
export function PickedList({
  task,
  refreshKey,
  load,
}: {
  task: string;
  refreshKey: string;
  load: (task: string) => Promise<PickedEpisodes>;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<PickedEpisodes | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    let live = true;
    const timer = setTimeout(() => {
      load(task)
        .then((value) => {
          if (live) {
            setData(value);
            setError("");
          }
        })
        .catch(
          (e) => live && setError(e instanceof Error ? e.message : String(e)),
        );
    }, 300);
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // load is rebuilt with the recipe; refreshKey stands for it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, task, refreshKey]);
  return (
    <details
      className="levi-pool-picked"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>{t("Show picked episodes")}</summary>
      {open && error && <p className="levi-error">{error}</p>}
      {open && !data && !error && (
        <p className="levi-pool-muted">{t("Computing…")}</p>
      )}
      {open && data && (
        <ol
          className="levi-pool-picked-list"
          aria-label={`${t("Picked episodes")}: ${task}`}
        >
          {data.episodes.map((row) => (
            <li key={row.key} title={row.sel_stratum}>
              <span className="levi-pool-picked-name">
                {row.viewer ? (
                  <Link className="text-cyan-300" href={row.viewer}>
                    {shortName(row.episode)} ↗
                  </Link>
                ) : (
                  <code className="levi-pool-path" title={row.episode}>
                    {shortName(row.episode)}
                  </code>
                )}
              </span>
              <span
                className={`levi-pool-outcome ${row.outcome || "none"}`}
                title={
                  row.outcome_source
                    ? t(
                        row.outcome_source === "human"
                          ? "human label"
                          : "robot flag",
                      )
                    : undefined
                }
              >
                {row.outcome ? t(row.outcome) : "—"}
              </span>
              <span className="tabular" title={t("Frames")}>
                {row.frames?.toLocaleString() ?? "—"}
              </span>
              <span className="tabular" title={t("Quality")}>
                {row.quality_score.toFixed(2)}
              </span>
              <span className="levi-pool-why">
                {row.selection_reason
                  .map((r) => t(PICK_REASONS[r] || r))
                  .join(" · ")}
              </span>
            </li>
          ))}
        </ol>
      )}
    </details>
  );
}

/** ``…/demo_0123`` -> ``demo_0123``. */
function shortName(episode: string): string {
  const parts = episode.split("/");
  return parts[parts.length - 1] || episode;
}

function share(n: number, total: number): string {
  return total ? `${Math.round((n / total) * 100)}%` : "—";
}

/** The composition's overall mix: successes and failures, categories and
 * how rollouts were run, so a lean shows before it is exported. */
export function MixSummary({ mix }: { mix: Mix }) {
  const { t } = useLocale();
  if (!mix.episodes) return null;
  const decided = mix.successes + mix.failures;
  return (
    <div className="levi-pool-mix">
      <h4>{t("Mix")}</h4>
      {decided > 0 && (
        <>
          <div
            className="levi-pool-mixbar"
            role="img"
            aria-label={`${t("successes")} ${mix.successes}, ${t("failures")} ${mix.failures}`}
          >
            <span
              className="success"
              style={{ width: `${(mix.successes / mix.episodes) * 100}%` }}
            />
            <span
              className="failure"
              style={{ width: `${(mix.failures / mix.episodes) * 100}%` }}
            />
          </div>
          <p className="levi-pool-hint tabular">
            {t("successes")} {mix.successes.toLocaleString()} (
            {share(mix.successes, decided)}) · {t("failures")}{" "}
            {mix.failures.toLocaleString()} ({share(mix.failures, decided)})
            {mix.unknown > 0
              ? ` · ${t("without outcome")} ${mix.unknown.toLocaleString()}`
              : ""}
          </p>
        </>
      )}
      {decided === 0 && (
        <p className="levi-pool-hint">{t("No outcomes recorded here.")}</p>
      )}
      <div className="levi-pool-constraints">
        {Object.entries(mix.categories).map(([c, n]) => (
          <span key={c} className="levi-pool-chip">
            {t(CATEGORY_NAMES[c] || c)} {share(n, mix.episodes)}
          </span>
        ))}
        {Object.entries(mix.policy_methods).map(([m, n]) => (
          <span key={m} className="levi-pool-chip">
            {t(METHOD_LABELS[m] || m)} {share(n, mix.episodes)}
          </span>
        ))}
      </div>
      {mix.lean && (
        <p className="levi-pool-warn" role="status">
          {t("The composition leans to one group")}:{" "}
          {t(
            mix.lean.dimension === "category"
              ? CATEGORY_NAMES[mix.lean.value] || mix.lean.value
              : METHOD_LABELS[mix.lean.value] || mix.lean.value,
          )}{" "}
          {Math.round(mix.lean.share * 100)}%.{" "}
          {t("Nothing is reweighted; adjust counts if that is not intended.")}
        </p>
      )}
    </div>
  );
}
