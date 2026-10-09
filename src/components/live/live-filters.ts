import { datasetOfSession, isLost, LIVE_STATES } from "./live-logic";
import type { DatasetRow, LiveSession } from "./types";

export type LiveStateFilter =
  | ""
  | "active"
  | "waiting"
  | "finished"
  | "problem"
  | "stopped"
  | "approval"
  | "idle";

export interface LiveFilters {
  model: string;
  task: string;
  state: LiveStateFilter;
  from: string;
  through: string;
  query: string;
}

export const EMPTY_LIVE_FILTERS: LiveFilters = {
  model: "",
  task: "",
  state: "",
  from: "",
  through: "",
  query: "",
};

/** The lightweight status row suffices for filtering; no detail fetch. */
export function datasetIdentity(name: string, row: DatasetRow) {
  const split = name.indexOf("__");
  return {
    model: row.group ?? (split < 0 ? name : name.slice(0, split)),
    task: row.task_folder ?? (split < 0 ? "" : name.slice(split + 2)),
  };
}

export function liveFilterOptions(
  sessions: LiveSession[],
  rows: Record<string, DatasetRow>,
): { models: string[]; tasks: string[] } {
  const models = new Set(sessions.map((s) => s.group));
  const tasks = new Set(sessions.map((s) => s.task_folder));
  for (const [name, row] of Object.entries(rows)) {
    const id = datasetIdentity(name, row);
    models.add(id.model);
    tasks.add(id.task);
  }
  const sorted = (values: Set<string>) =>
    [...values].filter(Boolean).sort((a, b) => a.localeCompare(b));
  return { models: sorted(models), tasks: sorted(tasks) };
}

function epoch(value: number | string | null | undefined): number {
  if (typeof value === "number") return Number.isFinite(value) ? value : 0;
  if (!value) return 0;
  const time = new Date(value).getTime() / 1000;
  return Number.isFinite(time) ? time : 0;
}

/** Local calendar bounds; the end date includes its whole day, also on DST. */
function dateBound(value: string, end: boolean): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const date = new Date(`${value}T00:00:00`);
  if (!Number.isFinite(date.getTime())) return null;
  if (end) date.setDate(date.getDate() + 1);
  return date.getTime() / 1000;
}

function matches(
  filters: LiveFilters,
  model: string,
  task: string,
  at: number,
  text: string,
): boolean {
  if (filters.model && filters.model !== model) return false;
  if (filters.task && filters.task !== task) return false;
  const from = dateBound(filters.from, false);
  const through = dateBound(filters.through, true);
  if (from != null && (!at || at < from)) return false;
  if (through != null && (!at || at >= through)) return false;
  const haystack = text.toLocaleLowerCase();
  return filters.query
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .every((word) => haystack.includes(word));
}

export function sessionActivity(session: LiveSession): number {
  return Math.max(
    epoch(session.started_at),
    epoch(session.updated_at),
    epoch(session.last_episode?.ended_at),
  );
}

function sessionStateMatches(s: LiveSession, state: LiveStateFilter): boolean {
  switch (state) {
    case "":
      return true;
    case "active":
      return s.state === "running" || s.state === "homing";
    case "waiting":
      return s.state === "standby" || s.state === "waiting_reset";
    case "finished":
      return s.state === "finished";
    case "problem":
      return (
        s.state === "fault" || s.state === "crashed" || !!s.fault || isLost(s)
      );
    case "stopped":
      return s.state === "stopped";
    default:
      return false;
  }
}

export function filterLiveSessions(
  sessions: LiveSession[],
  filters: LiveFilters,
): LiveSession[] {
  return sessions.filter(
    (s) =>
      sessionStateMatches(s, filters.state) &&
      matches(
        filters,
        s.group,
        s.task_folder,
        sessionActivity(s),
        [
          s.group,
          s.task_folder,
          s.prompt,
          s.run_id,
          s.session_id,
          s.reason,
          s.policy?.config,
          s.policy?.checkpoint,
        ].join(" "),
      ),
  );
}

function datasetStateMatches(row: DatasetRow, state: LiveStateFilter): boolean {
  switch (state) {
    case "":
      return true;
    case "active":
      return row.state === "annotating" || row.annotating > 0;
    case "waiting":
      return row.pending > 0 || (row.waiting ?? 0) > 0;
    case "finished":
      return row.episodes > 0 && row.done === row.episodes;
    case "problem":
      return row.state === "error" || row.failed > 0 || !!row.fault;
    case "approval":
      return row.state === "awaiting_approval";
    case "idle":
      return row.state === "idle" && row.pending === 0 && row.annotating === 0;
    default:
      return false;
  }
}

export function filterLiveDatasets(
  names: string[],
  rows: Record<string, DatasetRow>,
  sessions: LiveSession[],
  filters: LiveFilters,
): string[] {
  return names.filter((name) => {
    const row = rows[name];
    if (!row) return false;
    const id = datasetIdentity(name, row);
    const related = sessions.filter((s) => datasetOfSession(s) === name);
    const stateMatches =
      datasetStateMatches(row, filters.state) ||
      related.some((s) => sessionStateMatches(s, filters.state));
    if (!stateMatches) return false;
    const activity = Math.max(
      epoch(row.last_processed_at),
      epoch(row.updated_at),
      epoch(row.last_seen_at),
      ...related.map(sessionActivity),
    );
    return matches(
      filters,
      id.model,
      id.task,
      activity,
      [
        name,
        id.model,
        id.task,
        row.task_text,
        ...related.flatMap((s) => [s.prompt, s.run_id, s.policy?.checkpoint]),
      ].join(" "),
    );
  });
}

export function liveFiltersActive(filters: LiveFilters): boolean {
  return Object.values(filters).some((value) => value.trim() !== "");
}

/** Never offer a destructive action while a session is live or uncertain. */
export function sessionIsBusy(s: LiveSession): boolean {
  return LIVE_STATES.includes(s.state);
}
