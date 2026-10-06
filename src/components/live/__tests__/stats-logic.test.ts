import { describe, expect, test } from "bun:test";
import {
  columns,
  createStatsLoader,
  count,
  dueForRefresh,
  exportHref,
  factor,
  fill,
  hasOperatorLabels,
  hasPairs,
  kpis,
  percent,
  seconds,
  sessionChoices,
  statsPath,
  type StatsEpisode,
  type StatsSummary,
} from "../stats-logic";

describe("numbers", () => {
  test("seconds read as a short duration, unknown as a dash", () => {
    expect(seconds(4.26)).toBe("4.3 s");
    expect(seconds(0)).toBe("0 s");
    expect(seconds(125)).toBe("2 min 05 s");
    expect(seconds(3 * 3600 + 120)).toBe("3 h 02 min");
    for (const v of [null, undefined, NaN, Infinity])
      expect(seconds(v as number)).toBe("—");
  });
  test("counts, percentages and the real-time factor", () => {
    expect(count(297000)).toBe("297,000");
    expect(count(null)).toBe("—");
    expect(percent(0.857)).toBe("86%");
    expect(percent(0)).toBe("0%");
    expect(percent(undefined)).toBe("—");
    expect(factor(0.8571)).toBe("0.86×");
    expect(factor(null)).toBe("—");
  });
  test("placeholders are filled and a missing one is a dash", () => {
    expect(fill("{a} of {b}", { a: "3", b: "4" })).toBe("3 of 4");
    expect(fill("{a} of {b}", { a: "3" })).toBe("3 of —");
  });
});

describe("requests", () => {
  const none = { dataset: "", session: "" };
  test("the data request carries the scope and the page size", () => {
    expect(statsPath(none, 25)).toBe("stats?limit=25");
    expect(statsPath({ dataset: "g__t", session: "run 1/a" }, 50)).toBe(
      "stats?dataset=g__t&session=run+1%2Fa&limit=50",
    );
  });
  test("removed episodes are asked for only when switched on", () => {
    expect(
      statsPath(
        { dataset: "x__at__models", session: "", includeExcluded: true },
        25,
      ),
    ).toBe("stats?dataset=x__at__models&include_excluded=true&limit=25");
    expect(
      statsPath({ dataset: "", session: "", includeExcluded: false }, 25),
    ).toBe("stats?limit=25");
    expect(
      exportHref(
        "csv",
        { dataset: "", session: "", includeExcluded: true },
        "en",
      ),
    ).toBe(
      "/api/levi/live/stats/export?include_excluded=true&format=csv&lang=en",
    );
  });
  test("a dataset name with a root mark is encoded like any other", () => {
    expect(
      statsPath({ dataset: "a b__at__my models", session: "s/1" }, 5),
    ).toBe("stats?dataset=a+b__at__my+models&session=s%2F1&limit=5");
  });
  test("an export link names the format, the scope and the language", () => {
    expect(exportHref("csv", none, "en")).toBe(
      "/api/levi/live/stats/export?format=csv&lang=en",
    );
    expect(exportHref("md", { dataset: "g__t", session: "s1" }, "zh")).toBe(
      "/api/levi/live/stats/export?dataset=g__t&session=s1&format=md&lang=zh",
    );
    expect(exportHref("json", none, "fr")).toContain("lang=en");
  });
  test("it refreshes at once at first, then every 5 s while evaluating", () => {
    expect(dueForRefresh(1000, null, false)).toBe(true);
    expect(dueForRefresh(5999, 1000, true)).toBe(false);
    expect(dueForRefresh(6000, 1000, true)).toBe(true);
    expect(dueForRefresh(20000, 1000, false)).toBe(false);
    expect(dueForRefresh(31000, 1000, false)).toBe(true);
  });
});

const summary: StatsSummary = {
  episodes: { count: 14, done: 13, failed: 1 },
  latency: {
    to_commit_s: { n: 14, median: 27.6 },
    to_verdict_s: { n: 14, median: 28.4, p90: 33.9, max: 35.3 },
  },
  throughput: { realtime_factor: 0.2 },
  model: { tokens_total: 297000, tokens_per_episode: { mean: 21214 } },
  gpu: { closed_wait_s: 12.5, interruptions: 3 },
  in_session: { count: 0, evaluable: 14, ratio: 0 },
};

describe("key figures", () => {
  test("the seven headline numbers, in order", () => {
    const list = kpis(summary);
    expect(list.map((k) => k.id)).toEqual([
      "episodes",
      "median",
      "p90",
      "realtime",
      "tokens",
      "gate",
      "insession",
    ]);
    const byId = Object.fromEntries(list.map((k) => [k.id, k]));
    expect(byId.episodes.value).toBe("14");
    expect(byId.episodes.hintArgs).toEqual({ a: "13", b: "1" });
    expect(byId.median.value).toBe("28.4 s");
    expect(byId.median.hintArgs.a).toBe("27.6 s");
    expect(byId.p90.value).toBe("33.9 s");
    expect(byId.p90.hintArgs.a).toBe("35.3 s");
    expect(byId.realtime.value).toBe("0.2×");
    expect(byId.tokens.value).toBe("21,214");
    expect(byId.tokens.hintArgs.a).toBe("297,000");
    expect(byId.gate.value).toBe("12.5 s");
    expect(byId.gate.hintArgs.a).toBe("3");
    // A real zero is shown as 0%, not as a missing figure.
    expect(byId.insession.value).toBe("0%");
    expect(byId.insession.hintArgs).toEqual({ a: "0", b: "14" });
  });
  test("an empty or partial summary shows dashes, never zero or NaN", () => {
    for (const s of [undefined, {}, { episodes: { count: 0 } }]) {
      const list = kpis(s);
      expect(list).toHaveLength(7);
      const text = list.map((k) => k.value).join(" ");
      expect(text).not.toContain("NaN");
      expect(list.find((k) => k.id === "median")?.value).toBe("—");
      expect(list.find((k) => k.id === "insession")?.value).toBe("—");
    }
  });
});

describe("tables", () => {
  const row: StatsEpisode = {
    demo: "demo_0003",
    to_mirror_s: 6.9,
    to_first_request_s: 9,
    to_commit_s: 27.6,
    to_verdict_s: 28.4,
    closed_wait_s: 0,
    in_session: false,
    requests: 3,
    model_seconds: 11.2,
    total_tokens: 21034,
    images: 40,
    segments: 3,
    verdict: "failure",
    episode_seconds: 6,
  };
  test("the delay view and the cost view show their own columns", () => {
    const latency = columns("latency");
    expect(latency.map((c) => c.key)).toEqual([
      "demo",
      "to_mirror_s",
      "to_first_request_s",
      "to_commit_s",
      "to_verdict_s",
      "closed_wait_s",
      "in_session",
    ]);
    expect(latency.map((c) => c.cell(row))).toEqual([
      "demo_0003",
      "6.9 s",
      "9 s",
      "27.6 s",
      "28.4 s",
      "0 s",
      "No",
    ]);
    const cost = columns("cost");
    expect(cost.map((c) => c.cell(row))).toEqual([
      "demo_0003",
      "6 s",
      "3",
      "11.2 s",
      "21,034",
      "40",
      "3",
      "failure",
    ]);
  });
  test("the agent-vs-operator columns are hidden without operator labels", () => {
    for (const view of ["latency", "cost"] as const) {
      const keys = (rows: StatsEpisode[]) =>
        columns(view, rows).map((c) => c.key);
      expect(keys([row, { ...row, operator: "unlabeled" }])).toEqual(
        columns(view).map((c) => c.key),
      );
      expect(keys([row, { ...row, operator: null }])).toEqual(
        columns(view).map((c) => c.key),
      );
      expect(keys([])).not.toContain("operator");
      const shown = keys([row, { ...row, operator: "success" }]);
      expect(shown.slice(-2)).toEqual(["operator", "agreement"]);
    }
    expect(hasOperatorLabels(undefined)).toBe(false);
    const labelled = columns("cost", [{ ...row, operator: "success" }]);
    const cells = (r: StatsEpisode) => labelled.slice(-2).map((c) => c.cell(r));
    expect(cells({ ...row, operator: "success", agreement: "no" })).toEqual([
      "success",
      "disagrees",
    ]);
    expect(cells({ ...row, operator: "failure", agreement: "yes" })).toEqual([
      "failure",
      "agrees",
    ]);
    expect(
      cells({ ...row, operator: "success", agreement: "no_agent" }),
    ).toEqual(["success", "no agent verdict yet"]);
    expect(cells({ ...row, operator: "unlabeled", agreement: null })).toEqual([
      "unlabeled",
      "—",
    ]);
  });
  test("the session columns need a session with both labels", () => {
    expect(hasPairs([{ session: "s1" }, { session: "s2", pairs: 0 }])).toBe(
      false,
    );
    expect(hasPairs([{ session: "s1", pairs: 3 }])).toBe(true);
    expect(hasPairs(undefined)).toBe(false);
  });
  test("a row with nothing measured prints dashes", () => {
    for (const view of ["latency", "cost"] as const)
      for (const c of columns(view).slice(1)) expect(c.cell({})).toBe("—");
  });
  test("session choices follow the dataset and keep the chosen one", () => {
    const sessions = [
      { dataset: "a", session: "s1" },
      { dataset: "b", session: "s2" },
      { dataset: "a", session: "s1" },
      { dataset: "a", session: null },
    ];
    expect(sessionChoices(sessions, "", "")).toEqual(["s1", "s2"]);
    expect(sessionChoices(sessions, "b", "")).toEqual(["s2"]);
    expect(sessionChoices(sessions, "b", "gone")).toEqual(["s2", "gone"]);
  });
});

describe("the request follows the whole scope", () => {
  const answer = (body: unknown) => ({
    ok: true,
    status: 200,
    json: async () => body,
  });
  test("switching on removed episodes asks again with include_excluded", async () => {
    const asked: string[] = [];
    const loader = createStatsLoader(async (path) => {
      asked.push(path);
      return answer({ enabled: true });
    });
    const base = { dataset: "g__t", session: "", includeExcluded: false };
    await loader.load(base, 25);
    await loader.load({ ...base, includeExcluded: true }, 25);
    expect(asked).toEqual([
      "/api/levi/live/stats?dataset=g__t&limit=25",
      "/api/levi/live/stats?dataset=g__t&include_excluded=true&limit=25",
    ]);
    expect(loader.last()).toContain("include_excluded=true");
  });
  test("every part of the scope changes the request", () => {
    const paths = [
      { dataset: "", session: "", includeExcluded: false },
      { dataset: "a", session: "", includeExcluded: false },
      { dataset: "a", session: "s", includeExcluded: false },
      { dataset: "a", session: "s", includeExcluded: true },
    ].map((scope) => statsPath(scope, 25));
    expect(new Set(paths).size).toBe(4);
    expect(statsPath({ dataset: "", session: "" }, 25)).not.toBe(
      statsPath({ dataset: "", session: "" }, 50),
    );
  });
  test("the answer to an ask that a newer one replaced is dropped", async () => {
    let release: (v: unknown) => void = () => {};
    const slow = new Promise((resolve) => (release = resolve));
    let calls = 0;
    const loader = createStatsLoader(async () => {
      calls += 1;
      if (calls === 1) await slow;
      return answer({ enabled: true, n: calls });
    });
    const scope = { dataset: "", session: "" };
    const first = loader.load(scope, 25);
    const second = await loader.load({ ...scope, includeExcluded: true }, 25);
    release(null);
    expect(await first).toBeNull();
    expect(second).toEqual({ enabled: true, n: 2 });
  });
  test("a failed request rejects", async () => {
    const loader = createStatsLoader(async () => ({
      ok: false,
      status: 500,
      json: async () => ({}),
    }));
    await expect(loader.load({ dataset: "", session: "" }, 5)).rejects.toThrow(
      "HTTP 500",
    );
  });
});
