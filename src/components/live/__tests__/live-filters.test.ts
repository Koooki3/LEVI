import { describe, expect, test } from "bun:test";
import {
  EMPTY_LIVE_FILTERS,
  filterLiveDatasets,
  filterLiveSessions,
  liveFilterOptions,
  type LiveFilters,
} from "../live-filters";
import type { DatasetRow, LiveSession } from "../types";

const at = (value: string) => new Date(value).getTime() / 1000;
const sessions: LiveSession[] = [
  {
    group: "pi05",
    task_folder: "watermelon",
    state: "finished",
    started_at: at("2026-10-09T09:00:00"),
    prompt: "pick watermelon on bread",
    run_id: "run-1",
    policy: { checkpoint: "step49999" },
  },
  {
    group: "pi05",
    task_folder: "plates",
    state: "running",
    started_at: at("2026-10-09T10:00:00"),
    prompt: "stack plates",
    run_id: "run-2",
  },
  {
    group: "pi06",
    task_folder: "watermelon",
    state: "finished",
    started_at: at("2026-10-08T23:59:59"),
    prompt: "pick watermelon",
    run_id: "run-3",
  },
];
const row: DatasetRow = {
  episodes: 2,
  done: 2,
  pending: 0,
  annotating: 0,
  failed: 0,
  state: "idle",
};
const filters = (over: Partial<LiveFilters> = {}): LiveFilters => ({
  ...EMPTY_LIVE_FILTERS,
  ...over,
});

describe("live list filters", () => {
  test("combine model, task, status, date and all keyword terms", () => {
    const result = filterLiveSessions(
      sessions,
      filters({
        model: "pi05",
        task: "watermelon",
        state: "finished",
        from: "2026-10-09",
        through: "2026-10-09",
        query: "BREAD step49999",
      }),
    );
    expect(result.map((s) => s.run_id)).toEqual(["run-1"]);
    expect(
      filterLiveSessions(sessions, filters({ query: "bread impossible" })),
    ).toEqual([]);
    expect(sessions.length).toBe(3);
  });
  test("the end date includes its whole day and unknown dates never match a date range", () => {
    const data: LiveSession[] = [
      { ...sessions[0], run_id: "last", started_at: at("2026-10-09T23:59:59") },
      { ...sessions[0], run_id: "next", started_at: at("2026-10-10T00:00:00") },
      { ...sessions[0], run_id: "unknown", started_at: null },
    ];
    expect(
      filterLiveSessions(
        data,
        filters({ from: "2026-10-09", through: "2026-10-09" }),
      ).map((s) => s.run_id),
    ).toEqual(["last"]);
  });
  test("filter options and results include history beyond the old 64-session cut", () => {
    const data = Array.from({ length: 100 }, (_, i) => ({
      ...sessions[0],
      group: `model-${i}`,
      run_id: `run-${i}`,
    }));
    expect(liveFilterOptions(data, {}).models).toContain("model-99");
    expect(
      filterLiveSessions(data, filters({ model: "model-99" }))[0].run_id,
    ).toBe("run-99");
    expect(filterLiveSessions(data, filters())).toHaveLength(100);
  });
  test("pipelines filter using lightweight identity and text, with a legacy name fallback", () => {
    const rows = {
      arbitrary: {
        ...row,
        group: "pi05",
        task_folder: "watermelon",
        task_text: "pick on bread",
        last_seen_at: at("2026-10-09T10:00:00"),
      },
      pi06__plates: { ...row, last_processed_at: at("2026-10-09T10:00:00") },
    };
    expect(
      filterLiveDatasets(
        Object.keys(rows),
        rows,
        [],
        filters({
          model: "pi05",
          task: "watermelon",
          state: "finished",
          from: "2026-10-09",
          query: "bread",
        }),
      ),
    ).toEqual(["arbitrary"]);
    expect(
      filterLiveDatasets(
        Object.keys(rows),
        rows,
        [],
        filters({ model: "pi06", task: "plates" }),
      ),
    ).toEqual(["pi06__plates"]);
  });
  test("an active session retains its associated pipeline under the same status filter", () => {
    const rows = { pi05__plates: row, pi05__watermelon: row };
    expect(
      filterLiveDatasets(
        Object.keys(rows),
        rows,
        sessions,
        filters({ state: "active" }),
      ),
    ).toEqual(["pi05__plates"]);
    expect(liveFilterOptions(sessions, rows)).toEqual({
      models: ["pi05", "pi06"],
      tasks: ["plates", "watermelon"],
    });
  });
});
