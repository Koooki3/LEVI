import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { EpisodeTable } from "../pool-tables";
import type { EpisodeRow } from "../types";

describe("episode table outcome", () => {
  test("shows where the outcome comes from, in words", () => {
    const row = {
      key: "a/demo_0001",
      source: "a",
      source_path: "/data/a",
      category: "rollout",
      task: "t",
      episode: "demo_0001",
      canonical: true,
      exportable: true,
      outcome: "success",
      outcome_source: "robot_flag",
      frames: 10,
      format: "raw",
    } as unknown as EpisodeRow;
    const html = renderToStaticMarkup(
      createElement(EpisodeTable, {
        rows: [row],
        total: 1,
        offset: 0,
        pageSize: 50,
        chosenTasks: [],
        exclude: [],
        onPage: () => {},
        onToggleExclude: () => {},
      }),
    );
    expect(html).toContain("success");
    expect(html).toMatch(/pg-pool-outcome-source"> · robot flag</);
  });
});
