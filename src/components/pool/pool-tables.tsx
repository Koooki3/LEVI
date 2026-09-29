"use client";
import Link from "next/link";
import { useLocale } from "@/components/levi-locale";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  type EpisodeRow,
  type TaskRow,
} from "./types";

const CATEGORY_SHORT: Record<string, string> = {
  human: "Human",
  rollout: "Rollout",
  levi: "LEVI",
  external: "External",
  archive: "Archive",
};

/** Per task: episodes by category, frames and success rate; add a task to
 * the composition or focus the episode table on it. */
export function TaskTable({
  tasks,
  chosen,
  focus,
  onAdd,
  onFocus,
}: {
  tasks: TaskRow[];
  chosen: string[];
  focus: string | null;
  onAdd: (task: string) => void;
  onFocus: (task: string | null) => void;
}) {
  const { t } = useLocale();
  const columns = CATEGORIES.filter((c) =>
    tasks.some((row) => row.categories[c]),
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
            <th scope="col">
              <span className="sr-only">{t("Actions")}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((row) => {
            const added = chosen.includes(row.task);
            return (
              <tr
                key={row.task}
                className={focus === row.task ? "levi-pool-focus" : ""}
              >
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
                <td className="num tabular">{row.episodes.toLocaleString()}</td>
                <td className="num tabular">{row.frames.toLocaleString()}</td>
                <td
                  className="num tabular"
                  title={`${row.success} / ${row.success + row.failure}`}
                >
                  {row.success_rate === null
                    ? "—"
                    : `${Math.round(row.success_rate * 100)}%`}
                </td>
                <td>
                  <button
                    type="button"
                    className="levi-pool-add"
                    disabled={added}
                    aria-label={`${t("Add to composition")}: ${row.task}`}
                    onClick={() => onAdd(row.task)}
                  >
                    {added ? t("Added") : `+ ${t("Add")}`}
                  </button>
                </td>
              </tr>
            );
          })}
          {tasks.length === 0 && (
            <tr>
              <td colSpan={columns.length + 5} className="levi-pool-muted">
                {t("No tasks match these filters.")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
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
                  <td className="levi-pool-ellipsis" title={row.source_path}>
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
                <td colSpan={7} className="levi-pool-muted">
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
