"use client";
import { useColon } from "@/components/pages-ui/messages";
import { ArrowUpRight, Check, Pencil } from "lucide-react";
import { Button, Icon, SkeletonText } from "@/components/ds";
import { RequestProblem } from "@/components/pages-ui/feedback";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import { SelectionFields } from "./selection-fields";
import {
  METHOD_LABELS,
  OUTCOME_SOURCE_LABELS,
  gripperLabel,
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
  const colon = useColon();
  const [editing, setEditing] = useState(false);
  const short = report && (report.shortfall > 0 || report.notes.length > 0);
  return (
    <div className="pg-pool-pick">
      <div className="pg-pool-pick-line">
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
          <span className="pg-pool-muted">…</span>
        )}
        <span className="pg-pool-badge">
          <span className="sr-only">
            {t("How to pick")}
            {colon}
          </span>
          {t(STRATEGY_LABELS[entry.strategy])}
        </span>
        <Button
          size="sm"
          variant="ghost"
          icon={editing ? Check : Pencil}
          aria-expanded={editing}
          onClick={() => setEditing(!editing)}
        >
          {editing ? t("Done") : t("Edit")}
        </Button>
      </div>
      {short && report && (
        <ul className="pg-pool-notes">
          {report.shortfall > 0 && (
            <li className="pg-pool-warn">
              {t("Requested")} {report.requested?.toLocaleString()}, {t("only")}{" "}
              {report.selected.toLocaleString()} {t("available")}
            </li>
          )}
          {report.notes
            .filter((n) => n !== "fewer_available")
            .map((n) => (
              <li key={n} className="pg-pool-warn">
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
  const colon = useColon();
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
      className="pg-pool-picked"
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>{t("Show picked episodes")}</summary>
      {open && error && (
        <RequestProblem
          action="The picked episodes could not be listed"
          message={error}
        />
      )}
      {open && !data && !error && (
        <div className="pg-pool-picked-loading" aria-busy="true">
          <span className="sr-only">{t("Computing…")}</span>
          <SkeletonText lines={3} />
        </div>
      )}
      {open && data && (
        <ol
          className="pg-pool-picked-list"
          aria-label={`${t("Picked episodes")}${colon}${task}`}
        >
          {data.episodes.map((row) => (
            <li key={row.key} title={row.sel_stratum}>
              <span className="pg-pool-picked-name">
                {row.viewer ? (
                  <Link href={row.viewer}>
                    {shortName(row.episode)}
                    <Icon icon={ArrowUpRight} />
                  </Link>
                ) : (
                  <code className="pg-pool-path" title={row.episode}>
                    {shortName(row.episode)}
                  </code>
                )}
              </span>
              <span
                className={`pg-pool-outcome ${row.outcome || "none"}`}
                title={
                  row.outcome_source
                    ? t(
                        OUTCOME_SOURCE_LABELS[row.outcome_source] ||
                          "robot flag",
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
              <span className="pg-pool-why">
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

/** Which grippers the composition's episodes recorded; unknown ones (older
 * data, nothing in the metadata) are counted, not hidden. */
export function GripperMix({
  grippers,
  declared,
}: {
  grippers?: Record<string, number>;
  declared?: number;
}) {
  const { t } = useLocale();
  const entries = Object.entries(grippers || {}).filter(([, n]) => n > 0);
  if (!entries.length) return null;
  const known = entries.some(([g]) => g !== "unknown");
  return (
    <div className="pg-pool-mix">
      <h4>{t("Grippers")}</h4>
      <div className="pg-pool-constraints">
        {entries.map(([g, n]) => (
          <span key={g} className="pg-pool-chip">
            {gripperLabel(g, t)} {n.toLocaleString()}
          </span>
        ))}
      </div>
      {!!declared && (
        <p className="pg-pool-hint">
          {t(
            "{count} episode(s): the gripper comes from a declaration in pool/rules.json, not from the metadata.",
          ).replace("{count}", declared.toLocaleString())}
        </p>
      )}
      {!known && (
        <p className="pg-pool-hint">
          {t(
            "No gripper is recorded for these sources; their episodes export as unknown.",
          )}
        </p>
      )}
    </div>
  );
}

/** The composition's overall mix: successes and failures, categories and
 * how rollouts were run, so a lean shows before it is exported. */
export function MixSummary({ mix }: { mix: Mix }) {
  const { t } = useLocale();
  const colon = useColon();
  if (!mix.episodes) return null;
  const decided = mix.successes + mix.failures;
  return (
    <div className="pg-pool-mix">
      <h4>{t("Mix")}</h4>
      {decided > 0 && (
        <>
          <div
            className="pg-pool-mixbar"
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
          <p className="pg-pool-hint tabular">
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
        <p className="pg-pool-hint">{t("No outcomes recorded here.")}</p>
      )}
      <div className="pg-pool-constraints">
        {Object.entries(mix.categories).map(([c, n]) => (
          <span key={c} className="pg-pool-chip">
            {t(CATEGORY_NAMES[c] || c)} {share(n, mix.episodes)}
          </span>
        ))}
        {Object.entries(mix.policy_methods).map(([m, n]) => (
          <span key={m} className="pg-pool-chip">
            {t(METHOD_LABELS[m] || m)} {share(n, mix.episodes)}
          </span>
        ))}
      </div>
      {mix.lean && (
        <p className="pg-pool-warn" role="status">
          {t("The composition leans to one group")}
          {colon}
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
