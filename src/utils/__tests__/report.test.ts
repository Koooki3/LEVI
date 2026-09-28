import { describe, expect, test } from "bun:test";
import {
  type ReportStatus,
  assetSrc,
  cellText,
  chartSeries,
  clampProgress,
  deltaTone,
  extractHeadings,
  formatAge,
  formatDelta,
  formatDuration,
  formatEta,
  formatGpu,
  formatNumber,
  isFollowableLink,
  isReportBlock,
  lookupChart,
  lookupMetrics,
  lookupMilestones,
  lookupTable,
  lookupWorkstreams,
  normalizeState,
  parseBlock,
  pick,
  slugify,
  sortRows,
  sortWorkstreams,
} from "@/utils/report";

const status: ReportStatus = {
  schema: "levi.report.status.v1",
  workstreams: [
    { id: "W1", state: "done", progress: 1 },
    { id: "W2", state: "running", progress: 0.4 },
    { id: "W3", state: "odd", progress: 2 },
  ],
  metrics: { quality: [{ value: 0.9, baseline: 0.8, better: "higher" }] },
  charts: { speed: [{ x: "a", s: 1 }] },
  tables: { runs: { columns: [{ key: "a" }], rows: [{ a: 1 }] } },
  milestones: { plan: [{ date: "2026-09-26", state: "done" }] },
};

describe("bilingual fields", () => {
  test("pick the language, fall back to English", () => {
    expect(pick({ en: "Hi", zh: "你好" }, "zh")).toBe("你好");
    expect(pick({ en: "Hi" }, "zh")).toBe("Hi");
    expect(pick({ zh: "你好" }, "en")).toBe("你好");
    expect(pick("plain", "zh")).toBe("plain");
    expect(pick(null, "en")).toBe("");
  });
});

describe("fenced blocks", () => {
  test("recognises only the report languages", () => {
    expect(isReportBlock("levi-chart")).toBe(true);
    expect(isReportBlock("json")).toBe(false);
    expect(isReportBlock(null)).toBe(false);
  });

  test("malformed JSON is an error, not a throw", () => {
    const parsed = parseBlock("levi-chart", "{oops");
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toStartWith("Invalid JSON");
    expect(parseBlock("levi-table", "[1]").ok).toBe(false);
  });

  test("progress: all workstreams or one", () => {
    expect(parseBlock("levi-progress", '{"source":"workstreams"}')).toEqual({
      ok: true,
      spec: { kind: "progress", source: "workstreams" },
    });
    expect(parseBlock("levi-progress", '{"id":"W1b"}')).toEqual({
      ok: true,
      spec: { kind: "progress", id: "W1b" },
    });
    expect(parseBlock("levi-progress", '{"source":"other"}').ok).toBe(false);
  });

  test("chart: type, data reference or inline rows, series", () => {
    const parsed = parseBlock(
      "levi-chart",
      '{"type":"grouped-bar","data":"speed","series":[{"key":"s","label":{"en":"S"}}],"y_domain":[0,1]}',
    );
    expect(parsed.ok).toBe(true);
    if (parsed.ok && parsed.spec.kind === "chart") {
      expect(parsed.spec.x).toBe("x");
      expect(parsed.spec.y_domain).toEqual([0, 1]);
      expect(parsed.spec.data).toBe("speed");
    }
    const inline = parseBlock("levi-chart", '{"data":[{"x":1,"y":2}]}');
    expect(inline.ok && inline.spec.kind === "chart" && inline.spec.type).toBe(
      "bar",
    );
    expect(parseBlock("levi-chart", '{"type":"pie","data":"a"}').ok).toBe(
      false,
    );
    expect(parseBlock("levi-chart", '{"type":"bar"}').ok).toBe(false);
    expect(
      parseBlock("levi-chart", '{"data":"a","series":[{"label":"x"}]}').ok,
    ).toBe(false);
  });

  test("metrics, table and timeline need a data key", () => {
    expect(parseBlock("levi-metrics", '{"data":"quality"}').ok).toBe(true);
    expect(parseBlock("levi-table", '{"data":"runs"}').ok).toBe(true);
    expect(parseBlock("levi-timeline", '{"data":"plan"}').ok).toBe(true);
    expect(parseBlock("levi-metrics", "{}").ok).toBe(false);
  });
});

describe("status lookups", () => {
  test("workstreams: all, one, unknown", () => {
    expect(lookupWorkstreams(status, {})).toMatchObject({ ok: true });
    const one = lookupWorkstreams(status, { id: "W2" });
    expect(one.ok && one.value.map((w) => w.id)).toEqual(["W2"]);
    const none = lookupWorkstreams(status, { id: "W9" });
    expect(none.ok).toBe(false);
    expect(lookupWorkstreams(null, {})).toEqual({ ok: true, value: [] });
  });

  test("keys resolve, missing and malformed keys explain themselves", () => {
    expect(lookupMetrics(status, "quality").ok).toBe(true);
    expect(lookupChart(status, "speed").ok).toBe(true);
    expect(lookupTable(status, "runs").ok).toBe(true);
    expect(lookupMilestones(status, "plan").ok).toBe(true);
    const gone = lookupChart(status, "nope");
    expect(!gone.ok && gone.error).toBe('status.json has no charts "nope"');
    expect(lookupTable({ tables: { runs: [] as never } }, "runs").ok).toBe(
      false,
    );
    expect(lookupChart(null, "speed").ok).toBe(false);
    expect(lookupChart(null, [{ x: 1 }]).ok).toBe(true);
  });

  test("series default to numeric columns", () => {
    expect(
      chartSeries(
        [
          { x: "a", p: 1, q: "t" },
          { x: "b", r: 2 },
        ],
        "x",
        [],
      ),
    ).toEqual([{ key: "p" }, { key: "r" }]);
    expect(chartSeries([], "x", [{ key: "k" }])).toEqual([{ key: "k" }]);
  });

  test("workstream states and order", () => {
    expect(normalizeState("odd")).toBe("planned");
    expect(sortWorkstreams(status.workstreams!).map((w) => w.id)).toEqual([
      "W2",
      "W3",
      "W1",
    ]);
    expect(clampProgress(2)).toBe(1);
    expect(clampProgress(-1)).toBe(0);
    expect(clampProgress(null)).toBe(null);
  });
});

describe("number formats", () => {
  test("values by format", () => {
    expect(formatNumber(0.9364, "0.000")).toBe("0.936");
    expect(formatNumber(0.9364, "pct")).toBe("93.6%");
    expect(formatNumber(12345.6, "int")).toBe("12,346");
    expect(formatNumber(95, "seconds")).toBe("1 min 35 s");
    expect(formatNumber(95, "seconds", "zh")).toBe("1 分 35 秒");
    expect(formatNumber(0.123456, undefined)).toBe("0.1235");
    expect(formatNumber(null, "pct")).toBe("—");
  });

  test("durations", () => {
    expect(formatDuration(4.25, "en")).toBe("4.3 s");
    expect(formatDuration(42, "en")).toBe("42 s");
    expect(formatDuration(3600 + 5 * 60, "en")).toBe("1 h 05 min");
    expect(formatDuration(7200, "zh")).toBe("2 小时");
  });

  test("delta and its direction", () => {
    expect(formatDelta(0.936, 0.808, "pct")).toBe("+12.8 pp");
    expect(formatDelta(0.7, 0.8, "0.000")).toBe("−0.100");
    expect(formatDelta(60, 100, "seconds")).toBe("−40 s (−40%)");
    expect(formatDelta(1, null, "pct")).toBe(null);
    expect(deltaTone(0.9, 0.8, "higher")).toBe("good");
    expect(deltaTone(0.9, 0.8, "lower")).toBe("bad");
    expect(deltaTone(60, 100, "lower")).toBe("good");
    expect(deltaTone(1, 1, "higher")).toBe("neutral");
    expect(deltaTone(1, 2, undefined)).toBe("neutral");
  });

  test("times", () => {
    const now = Date.parse("2026-09-28T12:00:00Z");
    expect(formatAge("2026-09-28T11:57:00Z", now, "en")).toBe("3 min ago");
    expect(formatAge("2026-09-28T09:00:00Z", now, "zh")).toBe("3 小时前");
    expect(formatAge("2026-09-28T11:59:50Z", now, "en")).toBe("just now");
    expect(formatAge("garbage", now, "en")).toBe(null);
    expect(formatEta("2026-09-28T13:05:00Z", now, "en")).toEndWith(
      "· in 1 h 05 min",
    );
    expect(formatEta("tomorrow", now, "en")).toBe("tomorrow");
    expect(formatEta(null, now, "en")).toBe(null);
  });

  test("gpu", () => {
    expect(
      formatGpu({ gpu: { used_mib: 16384, total_mib: 32768, holder: "W1b" } }),
    ).toEqual({ text: "16.0 / 32.0 GiB", fraction: 0.5, holder: "W1b" });
    expect(formatGpu({})).toBe(null);
  });
});

describe("tables", () => {
  const rows = [
    { v: 10, n: "b2" },
    { v: null, n: "b10" },
    { v: 2, n: "a" },
  ];
  test("numbers numerically, text naturally, empty last", () => {
    expect(sortRows(rows, "v", "asc").map((r) => r.v)).toEqual([2, 10, null]);
    expect(sortRows(rows, "v", "desc").map((r) => r.v)).toEqual([10, 2, null]);
    expect(sortRows(rows, "n", "asc").map((r) => r.n)).toEqual([
      "a",
      "b2",
      "b10",
    ]);
    expect(sortRows(rows, null, "asc")).toBe(rows);
  });

  test("bilingual cells sort and show in the reader's language", () => {
    const cells = [
      { n: { en: "beta", zh: "乙" } },
      { n: { en: "alpha" } },
      { n: "gamma" },
    ];
    expect(
      sortRows(cells, "n", "asc", "en").map((r) => cellText(r.n, "en")),
    ).toEqual(["alpha", "beta", "gamma"]);
    expect(cellText({ en: "beta", zh: "乙" }, "zh")).toBe("乙");
    expect(cellText({ en: "alpha" }, "zh")).toBe("alpha");
    expect(cellText(0.5, "en")).toBe("0.5");
    expect(cellText(null, "en")).toBe("");
  });
});

describe("headings", () => {
  test("ids are unique, CJK kept, code fences skipped", () => {
    const md = [
      "# Title",
      "## Results **now**",
      "```md",
      "## not a heading",
      "```",
      "## Results now",
      "### 速度 与 质量",
      "~~~",
      "# hidden",
      "~~~",
      "## [Link](x) `code` ##",
    ].join("\n");
    expect(extractHeadings(md)).toEqual([
      { level: 1, text: "Title", id: "title", line: 1 },
      { level: 2, text: "Results now", id: "results-now", line: 2 },
      { level: 2, text: "Results now", id: "results-now-2", line: 6 },
      { level: 3, text: "速度 与 质量", id: "速度-与-质量", line: 7 },
      { level: 2, text: "Link code", id: "link-code", line: 11 },
    ]);
    expect(slugify("!!!")).toBe("section");
  });
});

describe("assets and links", () => {
  test("only assets/ goes through the report route", () => {
    expect(assetSrc("assets/ui-a.png")).toBe(
      "/api/levi/report/assets/ui-a.png",
    );
    expect(assetSrc("./assets/dir/a b.png")).toBe(
      "/api/levi/report/assets/dir/a%20b.png",
    );
    expect(assetSrc("assets/../status.json")).toBe(null);
    expect(assetSrc("figs/a.png")).toBe(null);
    expect(assetSrc("https://example.org/a.png")).toBe(
      "https://example.org/a.png",
    );
    expect(assetSrc("javascript:alert(1)")).toBe(null);
  });

  test("followable links", () => {
    expect(isFollowableLink("https://x")).toBe(true);
    expect(isFollowableLink("#section")).toBe(true);
    expect(isFollowableLink("reports/a.md")).toBe(false);
  });
});
