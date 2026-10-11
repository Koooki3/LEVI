import type { StatsResponse, StatsScope } from "./stats-logic";
import type { ResultItem } from "./result-types";

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/** Check the entire aggregate, never retain a summary after dropping foreign rows.
 * Dataset choices may include several canonical datasets, but the requested
 * session aggregate and every returned row must use the selected exact scope. */
export function sessionStatisticsMatch(
  value: unknown,
  scope: StatsScope,
  identity: ResultItem["identity"] | null,
): value is StatsResponse {
  if (!identity) return false;
  const canonicalDatasets = identity.datasets;
  if (
    !scope.dataset ||
    !scope.session ||
    !canonicalDatasets.includes(scope.dataset) ||
    scope.session !== (identity.run_id || identity.session_id)
  )
    return false;
  if (
    !object(value) ||
    value.enabled !== true ||
    value.schema !== "levi.live.stats.v1"
  )
    return false;
  // A session aggregate has one verified canonical source. Same dataset/run
  // names under another root cannot inherit an earlier index response's grant.
  if (
    !Array.isArray(value.source_identities) ||
    value.source_identities.length !== 1
  )
    return false;
  const source = value.source_identities[0];
  if (
    !object(source) ||
    typeof source.root !== "string" ||
    !source.root ||
    typeof source.group !== "string" ||
    !source.group ||
    typeof source.task !== "string" ||
    !source.task ||
    (source.session_id !== null &&
      (typeof source.session_id !== "string" || !source.session_id)) ||
    source.root !== identity.root ||
    source.group !== identity.group ||
    source.task !== identity.task ||
    typeof source.run_id !== "string" ||
    !source.run_id ||
    source.run_id !== identity.run_id ||
    source.session_id !== identity.session_id ||
    !Array.isArray(source.datasets) ||
    !source.datasets.includes(scope.dataset) ||
    !source.datasets.every(
      (dataset) =>
        typeof dataset === "string" &&
        !!dataset &&
        canonicalDatasets.includes(dataset),
    )
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
        typeof dataset === "string" &&
        !!dataset &&
        canonicalDatasets.includes(dataset),
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
