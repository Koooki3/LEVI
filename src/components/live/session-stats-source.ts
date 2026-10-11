import type { StatsResponse, StatsScope } from "./stats-logic";

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/** Check the entire aggregate, never retain a summary after dropping foreign rows.
 * Dataset choices may include several canonical datasets, but the requested
 * session aggregate and every returned row must use the selected exact scope. */
export function sessionStatisticsMatch(
  value: unknown,
  scope: StatsScope,
  canonicalDatasets: readonly string[],
): value is StatsResponse {
  if (
    !scope.dataset ||
    !scope.session ||
    !canonicalDatasets.includes(scope.dataset)
  )
    return false;
  if (
    !object(value) ||
    value.enabled !== true ||
    value.schema !== "levi.live.stats.v1"
  )
    return false;
  if (
    !object(value.scope) ||
    value.scope.dataset !== scope.dataset ||
    value.scope.session !== scope.session
  )
    return false;
  if (
    value.include_excluded !== !!scope.includeExcluded ||
    !object(value.summary)
  )
    return false;
  if (
    !Array.isArray(value.datasets) ||
    !value.datasets.every(
      (dataset) =>
        typeof dataset === "string" && canonicalDatasets.includes(dataset),
    )
  )
    return false;
  const rowMatches = (row: unknown) =>
    object(row) &&
    row.dataset === scope.dataset &&
    row.session === scope.session;
  return (
    Array.isArray(value.sessions) &&
    value.sessions.every(rowMatches) &&
    object(value.episodes) &&
    Array.isArray(value.episodes.rows) &&
    value.episodes.rows.every(rowMatches)
  );
}
