"use client";
import { Fragment, useState } from "react";
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import { SelectionFields, entryFromSuggest } from "./selection-fields";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  METHOD_LABELS,
  gripperDeclared,
  gripperLabel,
  gripperTitle,
  POLICY_METHODS,
  policyLabel,
  type EpisodeRow,
  type Suggest,
  type TaskEntry,
  type TaskRow,
} from "./types";

/** A task with more available episodes than this always opens the chooser. */
export const CHOOSER_ABOVE = 100;

const CATEGORY_SHORT: Record<string, string> = {
  human: "Human",
  rollout: "Rollout",
  levi: "LEVI",
  external: "External",
  archive: "Archive",
};

/** Per task: episodes by category, frames and success rate; add a task to
 * the composition (a task with many episodes opens a chooser for how many,
 * which outcomes and how to pick) or focus the episode table on it. */
export function TaskTable({
  tasks,
  chosen,
  focus,
  suggest,
  onAdd,
  onFocus,
}: {
  tasks: TaskRow[];
  chosen: string[];
  focus: string | null;
  suggest: (task: string) => Promise<Suggest>;
  onAdd: (entry: TaskEntry) => void;
  onFocus: (task: string | null) => void;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState<{
    task: string;
    info: Suggest;
    draft: TaskEntry;
  } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [problem, setProblem] = useState("");
  async function add(row: TaskRow) {
    setBusy(row.task);
    setProblem("");
    try {
      const info = await suggest(row.task);
      const draft = entryFromSuggest(row.task, info);
      // A small task the composition's balance does not trim goes in whole.
      if (info.available <= CHOOSER_ABOVE && draft.count === null) {
        onAdd(draft);
        setOpen(null);
      } else {
        setOpen({ task: row.task, info, draft });
      }
    } catch (e) {
      setProblem(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  const columns = CATEGORIES.filter((c) =>
    tasks.some((row) => row.categories[c]),
  );
  // Only when some task has rollouts.
  const showMethods = tasks.some(
    (row) => Object.keys(row.policy_methods || {}).length > 0,
  );
  return (
    <div className="levi-pool-table-wrap">
      <table className="levi-table levi-pool-table">
        <caption className="sr-only">{t("Tasks")}</caption>
        <thead>
          <tr>
            <th scope="col">{t("Task")}</th>
            {columns.map((c) => (
              <th
                scope="col"
                key={c}
                className="num"
                title={t(CATEGORY_LABELS[c])}
              >
                {t(CATEGORY_SHORT[c])}
              </th>
            ))}
            <th scope="col" className="num">
              {t("Episodes")}
            </th>
            <th scope="col" className="num">
              {t("Frames")}
            </th>
            <th scope="col" className="num">
              {t("Success rate")}
            </th>
            {showMethods && <th scope="col">{t("How it was run")}</th>}
            <th scope="col">
              <span className="sr-only">{t("Actions")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((row) => {
            const added = chosen.includes(row.task);
            const chooser = open?.task === row.task ? open : null;
            return (
              <Fragment key={row.task}>
                <tr className={focus === row.task ? "levi-pool-focus" : ""}>
                  <td>
                    <button
                      type="button"
                      className="levi-pool-task"
                      title={row.spellings.join("\n")}
                      aria-pressed={focus === row.task}
                      onClick={() =>
                        onFocus(focus === row.task ? null : row.task)
                      }
                    >
                      {row.task}
                    </button>
                    {row.spellings.length > 1 && (
                      <span className="levi-pool-muted">
                        {" "}
                        · {row.spellings.length} {t("spellings")}
                      </span>
                    )}
                  </td>
                  {columns.map((c) => (
                    <td key={c} className="num tabular">
                      {row.categories[c]
                        ? row.categories[c].toLocaleString()
                        : "·"}
                    </td>
                  ))}
                  <td className="num tabular">
                    {row.episodes.toLocaleString()}
                  </td>
                  <td className="num tabular">{row.frames.toLocaleString()}</td>
                  <td
                    className="num tabular"
                    title={`${row.success} / ${row.success + row.failure}`}
                  >
                    {row.success_rate === null
                      ? "—"
                      : `${Math.round(row.success_rate * 100)}%`}
                  </td>
                  {showMethods && (
                    <td className="levi-pool-methods">
                      {POLICY_METHODS.filter(
                        (m) => row.policy_methods?.[m],
                      ).map((m) => (
                        <span
                          key={m}
                          className="levi-pool-badge"
                          title={t(METHOD_LABELS[m])}
                        >
                          {t(METHOD_LABELS[m])} {row.policy_methods?.[m]}
                        </span>
                      ))}
                    </td>
                  )}
                  <td>
                    <button
                      type="button"
                      className="levi-pool-add"
                      disabled={added || busy === row.task}
                      aria-expanded={chooser ? true : undefined}
                      aria-label={`${t("Add to composition")}: ${row.task}`}
                      onClick={() => void add(row)}
                    >
                      {added ? t("Added") : `+ ${t("Add")}`}
                    </button>
                  </td>
                </tr>
                {chooser && (
                  <tr className="levi-pool-chooser-row">
                    <td colSpan={columns.length + 5 + (showMethods ? 1 : 0)}>
                      <div
                        className="levi-pool-chooser"
                        role="group"
                        aria-label={`${t("Choose episodes")}: ${row.task}`}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") setOpen(null);
                        }}
                      >
                        <strong>{t("Choose episodes")}</strong>
                        <SelectionFields
                          available={chooser.info.available}
                          successes={chooser.info.successes}
                          failures={chooser.info.failures}
                          unknown={chooser.info.unknown}
                          value={chooser.draft}
                          onChange={(patch) =>
                            setOpen({
                              ...chooser,
                              draft: { ...chooser.draft, ...patch },
                            })
                          }
                        />
                        {chooser.info.earlier_counts.length > 0 && (
                          <p className="levi-pool-hint">
                            {t(
                              "Suggested count: the median of the tasks already added.",
                            )}
                          </p>
                        )}
                        <div className="levi-row">
                          <button
                            type="button"
                            className="levi-primary"
                            autoFocus
                            onClick={() => {
                              onAdd(chooser.draft);
                              setOpen(null);
                            }}
                          >
                            {t("Add to composition")}
                          </button>
                          <button
                            type="button"
                            className="levi-secondary"
                            onClick={() => setOpen(null)}
                          >
                            {t("Cancel")}
                          </button>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
          {tasks.length === 0 && (
            <tr>
              <td
                colSpan={columns.length + 5 + (showMethods ? 1 : 0)}
                className="levi-pool-muted"
              >
                {t("No tasks match these filters.")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {problem && (
        <p className="levi-error" role="alert">
          {problem}
        </p>
      )}
    </div>
  );
}

function OutcomeCell({ row }: { row: EpisodeRow }) {
  const { t } = useLocale();
  if (!row.outcome) return <span className="levi-pool-muted">—</span>;
  const source =
    row.outcome_source === "human" ? t("human label") : t("robot flag");
  return (
    <span
      className={`levi-pool-outcome ${row.outcome}`}
      title={`${t(row.outcome)} · ${source}`}
    >
      {t(row.outcome)}
      {row.human_label ? " ✓" : ""}
    </span>
  );
}

/** Paged episodes with source, category, task, frames, outcome and
 * held-out / copy badges. Held-out rows can never be included. */
export function EpisodeTable({
  rows,
  total,
  offset,
  pageSize,
  chosenTasks,
  exclude,
  onPage,
  onToggleExclude,
}: {
  rows: EpisodeRow[];
  total: number;
  offset: number;
  pageSize: number;
  chosenTasks: string[];
  exclude: string[];
  onPage: (offset: number) => void;
  onToggleExclude: (key: string) => void;
}) {
  const { t } = useLocale();
  const last = Math.min(total, offset + rows.length);
  // Only when some listed episode is a rollout with policy information.
  const showPolicy = rows.some((row) => row.policy_method || row.policy_model);
  return (
    <div>
      <div className="levi-pool-table-wrap">
        <table className="levi-table levi-pool-table">
          <caption className="sr-only">{t("Episodes")}</caption>
          <thead>
            <tr>
              <th scope="col">{t("In export")}</th>
              <th scope="col">{t("Source")}</th>
              <th scope="col">{t("Category")}</th>
              <th scope="col">{t("Task")}</th>
              {showPolicy && <th scope="col">{t("Policy")}</th>}
              <th scope="col">{t("Gripper")}</th>
              <th scope="col" className="num">
                {t("Frames")}
              </th>
              <th scope="col">{t("Outcome")}</th>
              <th scope="col">{t("Episode")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const inComposition = chosenTasks.includes(row.task);
              return (
                <tr key={row.key}>
                  <td>
                    {row.heldout ? (
                      <span
                        className="levi-pool-badge heldout"
                        title={t("Held-out episodes are never exported")}
                      >
                        {t("held-out")}
                      </span>
                    ) : inComposition ? (
                      <input
                        type="checkbox"
                        aria-label={`${t("Include in export")}: ${row.key}`}
                        checked={!exclude.includes(row.key)}
                        onChange={() => onToggleExclude(row.key)}
                      />
                    ) : (
                      <span className="levi-pool-muted">·</span>
                    )}
                  </td>
                  <td className="levi-pool-source" title={row.source_path}>
                    {row.source}
                  </td>
                  <td>
                    {t(CATEGORY_SHORT[row.category] || row.category)}
                    {!row.canonical && (
                      <span className="levi-pool-badge">{t("copy")}</span>
                    )}
                    {row.nonstandard && (
                      <span className="levi-pool-badge warn">
                        {t("non-standard")}
                      </span>
                    )}
                    {!row.exportable && !row.nonstandard && (
                      <span className="levi-pool-badge warn">
                        {t("not exportable")}
                      </span>
                    )}
                  </td>
                  <td
                    className="levi-pool-ellipsis"
                    title={row.task_raw || row.task}
                  >
                    {row.task}
                  </td>
                  {showPolicy && (
                    <td
                      className="levi-pool-policy"
                      title={
                        [
                          row.policy_model,
                          row.policy_checkpoint,
                          row.policy_phase,
                        ]
                          .filter(Boolean)
                          .join("\n") || undefined
                      }
                    >
                      {policyLabel(row, t) || (
                        <span className="levi-pool-muted">—</span>
                      )}
                    </td>
                  )}
                  <td
                    className={
                      row.gripper && row.gripper !== "unknown"
                        ? undefined
                        : "levi-pool-muted"
                    }
                    title={gripperTitle(row, t)}
                  >
                    {gripperLabel(row.gripper, t)}
                    {gripperDeclared(row) && (
                      <span className="levi-pool-badge">{t("declared")}</span>
                    )}
                  </td>
                  <td className="num tabular">
                    {row.frames?.toLocaleString() ?? "—"}
                  </td>
                  <td>
                    <OutcomeCell row={row} />
                  </td>
                  <td className="levi-pool-ellipsis">
                    {row.viewer ? (
                      <Link className="text-cyan-300" href={row.viewer}>
                        {row.episode} ↗
                      </Link>
                    ) : (
                      <code
                        className="levi-pool-path"
                        title={`${row.source_path}/${row.episode}`}
                      >
                        {row.format === "lerobot"
                          ? `${row.source_path} #${row.episode}`
                          : `${row.source_path}/${row.episode}`}
                      </code>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={showPolicy ? 9 : 8} className="levi-pool-muted">
                  {t("No episodes match these filters.")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <nav className="levi-pool-pager" aria-label={t("Pages")}>
        <button
          type="button"
          className="levi-secondary"
          disabled={offset === 0}
          onClick={() => onPage(Math.max(0, offset - pageSize))}
        >
          ← {t("Previous page")}
        </button>
        <span className="tabular text-xs">
          {total ? `${offset + 1}–${last}` : "0"} / {total.toLocaleString()}
        </span>
        <button
          type="button"
          className="levi-secondary"
          disabled={offset + pageSize >= total}
          onClick={() => onPage(offset + pageSize)}
        >
          {t("Next page")} →
        </button>
      </nav>
    </div>
  );
}
