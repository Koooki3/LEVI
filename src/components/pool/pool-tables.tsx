"use client";
import {
  ArrowUpRight,
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Lock,
  Plus,
  UserCheck,
} from "lucide-react";
import { Badge, Button, Icon, StatusDot, Tooltip } from "@/components/ds";
import { RequestProblem } from "@/components/pages-ui/feedback";
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
    <div
      className="pg-pool-table-wrap"
      tabIndex={0}
      role="region"
      aria-label={t("Scrollable table")}
    >
      <table className="ds-table ds-table--compact pg-pool-table">
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
                <tr className={focus === row.task ? "pg-pool-focus" : ""}>
                  <td>
                    <button
                      type="button"
                      className="pg-pool-task"
                      title={row.spellings.join("\n")}
                      aria-pressed={focus === row.task}
                      onClick={() =>
                        onFocus(focus === row.task ? null : row.task)
                      }
                    >
                      {row.task}
                    </button>
                    {row.spellings.length > 1 && (
                      <span className="pg-pool-muted">
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
                    <td className="pg-pool-methods">
                      {POLICY_METHODS.filter(
                        (m) => row.policy_methods?.[m],
                      ).map((m) => (
                        <span key={m} className="pg-pool-badge">
                          {t(METHOD_LABELS[m])} {row.policy_methods?.[m]}
                        </span>
                      ))}
                    </td>
                  )}
                  <td>
                    <Button
                      size="sm"
                      variant={added ? "ghost" : "secondary"}
                      icon={added ? Check : Plus}
                      loading={busy === row.task}
                      disabled={added}
                      aria-expanded={chooser ? true : undefined}
                      aria-label={`${t("Add to composition")}: ${row.task}`}
                      onClick={() => void add(row)}
                    >
                      {added ? t("Added") : t("Add")}
                    </Button>
                  </td>
                </tr>
                {chooser && (
                  <tr className="pg-pool-chooser-row">
                    <td colSpan={columns.length + 5 + (showMethods ? 1 : 0)}>
                      <div
                        className="pg-pool-chooser"
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
                          <p className="pg-pool-hint">
                            {t(
                              "Suggested count: the median of the tasks already added.",
                            )}
                          </p>
                        )}
                        <div className="pg-row">
                          <Button
                            variant="primary"
                            icon={Plus}
                            autoFocus
                            onClick={() => {
                              onAdd(chooser.draft);
                              setOpen(null);
                            }}
                          >
                            {t("Add to composition")}
                          </Button>
                          <Button variant="ghost" onClick={() => setOpen(null)}>
                            {t("Cancel")}
                          </Button>
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
                className="pg-pool-muted"
              >
                {t("No tasks match these filters.")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {problem && (
        <RequestProblem
          action="The task could not be added"
          message={problem}
        />
      )}
    </div>
  );
}

function OutcomeCell({ row }: { row: EpisodeRow }) {
  const { t } = useLocale();
  if (!row.outcome) return <span className="pg-pool-muted">—</span>;
  const source =
    row.outcome_source === "human" ? t("human label") : t("robot flag");
  return (
    <StatusDot
      tone={
        row.outcome === "success"
          ? "success"
          : row.outcome === "failure"
            ? "danger"
            : "neutral"
      }
      className="pg-pool-outcome"
    >
      {t(row.outcome)}
      <span className="pg-pool-outcome-source"> · {source}</span>
      {row.human_label && <Icon icon={UserCheck} label={t("human label")} />}
    </StatusDot>
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
      <div
        className="pg-pool-table-wrap"
        tabIndex={0}
        role="region"
        aria-label={t("Scrollable table")}
      >
        <table className="ds-table ds-table--compact pg-pool-table">
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
                      <Tooltip
                        content={t("Held-out episodes are never exported")}
                      >
                        <span tabIndex={0} className="pg-badge-trigger">
                          <Badge tone="warning" icon={Lock}>
                            {t("held-out")}
                          </Badge>
                        </span>
                      </Tooltip>
                    ) : inComposition ? (
                      <input
                        type="checkbox"
                        aria-label={`${t("Include in export")}: ${row.key}`}
                        checked={!exclude.includes(row.key)}
                        onChange={() => onToggleExclude(row.key)}
                      />
                    ) : (
                      <span className="pg-pool-muted">·</span>
                    )}
                  </td>
                  <td className="pg-pool-source" title={row.source_path}>
                    {row.source}
                  </td>
                  <td>
                    {t(CATEGORY_SHORT[row.category] || row.category)}
                    {!row.canonical && (
                      <Badge icon={Copy} className="pg-badge-gap">
                        {t("copy")}
                      </Badge>
                    )}
                    {row.nonstandard && (
                      <Badge tone="warning" className="pg-badge-gap">
                        {t("non-standard")}
                      </Badge>
                    )}
                    {!row.exportable && !row.nonstandard && (
                      <Badge tone="warning" className="pg-badge-gap">
                        {t("not exportable")}
                      </Badge>
                    )}
                  </td>
                  <td
                    className="pg-pool-ellipsis"
                    title={row.task_raw || row.task}
                  >
                    {row.task}
                  </td>
                  {showPolicy && (
                    <td
                      className="pg-pool-policy"
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
                        <span className="pg-pool-muted">—</span>
                      )}
                    </td>
                  )}
                  <td
                    className={
                      row.gripper && row.gripper !== "unknown"
                        ? undefined
                        : "pg-pool-muted"
                    }
                    title={gripperTitle(row, t)}
                  >
                    {gripperLabel(row.gripper, t)}
                    {gripperDeclared(row) && (
                      <span className="pg-pool-badge">{t("declared")}</span>
                    )}
                  </td>
                  <td className="num tabular">
                    {row.frames?.toLocaleString() ?? "—"}
                  </td>
                  <td>
                    <OutcomeCell row={row} />
                  </td>
                  <td className="pg-pool-ellipsis">
                    {row.viewer ? (
                      <Link href={row.viewer}>
                        {row.episode}
                        <Icon icon={ArrowUpRight} />
                      </Link>
                    ) : (
                      <code
                        className="pg-pool-path"
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
                <td colSpan={showPolicy ? 9 : 8} className="pg-pool-muted">
                  {t("No episodes match these filters.")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <nav className="pg-pool-pager" aria-label={t("Pages")}>
        <Button
          size="sm"
          icon={ChevronLeft}
          disabled={offset === 0}
          onClick={() => onPage(Math.max(0, offset - pageSize))}
        >
          {t("Previous page")}
        </Button>
        <span className="tabular pg-small">
          {total ? `${offset + 1}–${last}` : "0"} / {total.toLocaleString()}
        </span>
        <Button
          size="sm"
          iconEnd={ChevronRight}
          disabled={offset + pageSize >= total}
          onClick={() => onPage(offset + pageSize)}
        >
          {t("Next page")}
        </Button>
      </nav>
    </div>
  );
}
