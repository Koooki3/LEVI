import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  EpisodesTable,
  ExportLinks,
  KeyFigures,
  SessionsTable,
  StatsEmpty,
  StatsView,
} from "../stats-panel";
import type { StatsResponse } from "../stats-logic";

const data: StatsResponse = {
  enabled: true,
  datasets: ["g__t", "g__u"],
  scope: { dataset: null, session: null },
  summary: {
    episodes: { count: 2, done: 2, failed: 0 },
    latency: { to_verdict_s: { median: 28.4, p90: 33, max: 35 } },
    model: { tokens_total: 4200, tokens_per_episode: { mean: 2100 } },
    in_session: { count: 1, evaluable: 2, ratio: 0.5 },
  },
  sessions: [
    {
      dataset: "g__t",
      session: "run-A",
      episodes: 2,
      to_verdict_median_s: 28.4,
      tokens: 4200,
      realtime_factor: 0.5,
      in_session_ratio: 0.5,
      last_at: 1790000000,
    },
  ],
  episodes: {
    total: 3,
    rows: [
      {
        dataset: "g__t",
        demo: "demo_0001",
        to_commit_s: 20,
        total_tokens: 2000,
      },
      {
        dataset: "g__t",
        demo: "demo_0000",
        to_commit_s: 21,
        total_tokens: 2200,
      },
    ],
  },
};

const noop = () => {};

describe("statistics panel render", () => {
  test("the key figures show the numbers and their hints", () => {
    const html = renderToStaticMarkup(
      createElement(KeyFigures, { summary: data.summary }),
    );
    expect(html).toContain("Median delay");
    expect(html).toContain("28.4 s");
    expect(html).toContain("2,100");
    expect(html).toContain("4,200 in total");
    expect(html).toContain("50%");
    expect(html).toContain("1 of 2 episodes");
  });

  test("the session and episode tables list their rows", () => {
    const sessions = renderToStaticMarkup(
      createElement(SessionsTable, { rows: data.sessions ?? [] }),
    );
    expect(sessions).toContain("run-A");
    expect(sessions).toContain("0.5×");
    const episodes = renderToStaticMarkup(
      createElement(EpisodesTable, {
        data: data.episodes!,
        view: "latency",
        onView: noop,
        onMore: noop,
      }),
    );
    expect(episodes).toContain("demo_0001");
    expect(episodes).toContain("(2 / 3)");
    expect(episodes).toContain("Show more"); // 2 of 3 are loaded
    const cost = renderToStaticMarkup(
      createElement(EpisodesTable, {
        data: { ...data.episodes!, total: 2 },
        view: "cost",
        onView: noop,
        onMore: noop,
      }),
    );
    expect(cost).toContain("2,200");
    expect(cost).not.toContain("Show more");
  });

  test("export links point at the read-only export route", () => {
    const html = renderToStaticMarkup(
      createElement(ExportLinks, {
        scope: { dataset: "g__t", session: "run-A" },
      }),
    );
    for (const format of ["md", "json", "csv"])
      expect(html).toContain(
        `/api/levi/live/stats/export?dataset=g__t&amp;session=run-A&amp;format=${format}&amp;lang=en`,
      );
    expect(html).toContain("download");
  });

  test("the whole view has the scope selectors, the tables and a note", () => {
    const html = renderToStaticMarkup(
      createElement(StatsView, {
        data,
        scope: { dataset: "", session: "" },
        onScope: noop,
        view: "latency",
        onView: noop,
        onMore: noop,
        choices: ["run-A"],
      }),
    );
    expect(html).toContain("All datasets");
    expect(html).toContain("g__u");
    expect(html).toContain("All sessions");
    expect(html).toContain("Evaluation sessions");
    expect(html).toContain("Per episode");
    expect(html).toContain("a dash means the figure was not measured");
    // Both selectors are outlined fields (a bare select has no boundary).
    const selects = html.match(/<select[^>]*>/g) ?? [];
    expect(selects.length).toBe(2);
    for (const select of selects) expect(select).toContain("ds-input");
  });

  test("the switch for removed episodes is there and says how many are left out", () => {
    const render = (over: Partial<StatsResponse>, includeExcluded: boolean) =>
      renderToStaticMarkup(
        createElement(StatsView, {
          data: { ...data, ...over },
          scope: { dataset: "", session: "", includeExcluded },
          onScope: noop,
          view: "latency",
          onView: noop,
          onMore: noop,
          choices: [],
        }),
      );
    const off = render({ excluded_demos: 2 }, false);
    expect(off).toContain("Include removed episodes");
    expect(off).toContain("2 removed episode(s) in this scope");
    expect(off).not.toContain("checked");
    expect(render({ excluded_demos: 0 }, false)).toContain(
      "none removed in this scope",
    );
    const on = render({ excluded_demos: 2 }, true);
    expect(on).toContain("checked");
    expect(on).toContain("include_excluded=true");
  });

  test("a removed episode is tagged in the table", () => {
    const html = renderToStaticMarkup(
      createElement(EpisodesTable, {
        data: {
          total: 1,
          rows: [{ dataset: "g__t", demo: "demo_0004", excluded: true }],
        },
        view: "latency",
        onView: noop,
        onMore: noop,
      }),
    );
    expect(html).toContain("demo_0004");
    expect(html).toContain("removed");
  });

  test("with nothing recorded the panel says why and how to fill it", () => {
    const first = renderToStaticMarkup(
      createElement(StatsEmpty, { filtered: false }),
    );
    expect(first).toContain("No statistics yet");
    expect(first).toContain("levi live stats backfill");
    const narrowed = renderToStaticMarkup(
      createElement(StatsEmpty, { filtered: true }),
    );
    expect(narrowed).toContain("No labelled episode matches this scope.");
  });
});

describe("agent vs operator in the tables", () => {
  const sessions = data.sessions ?? [];
  test("no agreement columns without a session that has both labels", () => {
    const html = renderToStaticMarkup(
      createElement(SessionsTable, { rows: sessions }),
    );
    expect(html).not.toContain(">Agree<");
    expect(html).not.toContain(">False success<");
  });
  test("the columns, with counts, once a session has them", () => {
    const html = renderToStaticMarkup(
      createElement(SessionsTable, {
        rows: [
          ...sessions,
          {
            dataset: "g__t",
            session: "run-B",
            episodes: 4,
            pairs: 4,
            agree: 2,
            judged: 3,
            false_success: 1,
          },
        ],
      }),
    );
    expect(html).toContain(">Agree<");
    expect(html).toContain(">False success<");
    expect(html).toContain(">2/3<");
  });
  test("the episode table adds Operator and Agree only with a label", () => {
    const rows = [{ dataset: "g__t", demo: "demo_0001", verdict: "success" }];
    const render = (extra: object) =>
      renderToStaticMarkup(
        createElement(EpisodesTable, {
          data: { total: 1, rows: [{ ...rows[0], ...extra }] },
          view: "cost",
          onView: () => {},
          onMore: () => {},
        }),
      );
    expect(render({})).not.toContain(">Operator<");
    const shown = render({ operator: "failure", agreement: "no" });
    expect(shown).toContain(">Operator<");
    expect(shown).toContain(">disagrees<");
  });
});
