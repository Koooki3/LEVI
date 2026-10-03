import { describe, expect, test } from "bun:test";
import {
  columns,
  count,
  dueForRefresh,
  exportHref,
  factor,
  fill,
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
