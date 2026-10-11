import type { StatsResponse } from "../stats-logic";

/** Shape of levi.live.statsview.build; every positive test keeps provenance. */
export function statisticsFixture(
  overrides: StatsResponse = {},
): StatsResponse {
  const value: StatsResponse = {
    enabled: true,
    schema: "levi.live.stats.v1",
    generated_at: 100,
    scope: { dataset: "model__task", session: "run-1" },
    include_excluded: false,
    datasets: ["model__task"],
    summary: { episodes: { count: 0 } },
    sessions: [],
    sessions_total: 0,
    episodes: { rows: [], total: 0, offset: 0, limit: 25 },
    ...overrides,
  };
  return {
    ...value,
    episodes: value.episodes && {
      ...value.episodes,
      rows: value.episodes.rows?.map((row) => ({
        dataset: "model__task",
        session: "run-1",
        ...row,
      })),
    },
  };
}
