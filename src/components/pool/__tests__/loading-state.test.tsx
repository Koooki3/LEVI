import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { EpisodeTable } from "../pool-tables";
import { EMPTY_FILTERS, FacetsPanel } from "../facets-panel";
import type { EpisodeRow, Facets } from "../types";

const row = (n: number) =>
  ({
    key: `a/demo_${n}`,
    source: "a",
    source_path: "/data/a",
    category: "human",
    task: "t",
    episode: `demo_${n}`,
    canonical: true,
    exportable: true,
    frames: 10,
    format: "raw",
  }) as unknown as EpisodeRow;

const table = (extra: Record<string, unknown>) =>
  renderToStaticMarkup(
    createElement(EpisodeTable, {
      rows: [row(1), row(2)],
      total: 120,
      offset: 50,
      pageSize: 50,
      chosenTasks: [],
      exclude: [],
      onPage: () => {},
      onToggleExclude: () => {},
      ...extra,
    }),
  );

describe("the episode table while a page is on its way", () => {
  test("the range is read from the page shown, and the table is marked busy", () => {
    const html = table({ loadedOffset: 0, loading: true });
    expect(html).toContain("1–2");
    expect(html).not.toContain("51–");
    expect(html).toMatch(/aria-busy="true"/);
    expect(html).toContain("pg-pool-updating");
  });

  test("settled: the range follows the offset and nothing is busy", () => {
    const html = table({ loadedOffset: 50, loading: false });
    expect(html).toContain("51–52");
    expect(html).toMatch(/aria-busy="false"/);
    expect(html).not.toContain("pg-pool-updating");
  });

  test("before the first response the range and total are unknown, not 0", () => {
    const html = table({ rows: [], total: 0, offset: 0, loaded: false });
    expect(html).toMatch(/>…<?[^<]*\/[^<]*…/);
    expect(html).not.toContain("0 / 0");
  });
});

describe("facet counts", () => {
  const panel = (facets: Facets | null) =>
    renderToStaticMarkup(
      createElement(FacetsPanel, {
        facets,
        filters: EMPTY_FILTERS,
        onChange: () => {},
      }),
    );
  const counts = (html: string) =>
    [...html.matchAll(/pg-pool-count">([^<]*)</g)].map((m) => m[1]);

  test("unknown before the first response", () => {
    const shown = counts(panel(null));
    expect(shown.length).toBeGreaterThan(3);
    expect(new Set(shown)).toEqual(new Set(["…"]));
  });

  test("a known zero is 0", () => {
    const facets = {
      episodes: 0,
      categories: {},
      outcomes: {},
      sources: [],
    } as unknown as Facets;
    const shown = counts(panel(facets));
    expect(shown).toContain("0");
    expect(shown).not.toContain("…");
  });
});

describe("the In export checkbox", () => {
  const box = (over: Partial<EpisodeRow>) =>
    renderToStaticMarkup(
      createElement(EpisodeTable, {
        rows: [{ ...row(1), ...over }],
        total: 1,
        offset: 0,
        pageSize: 50,
        chosenTasks: ["t"],
        exclude: [],
        onPage: () => {},
        onToggleExclude: () => {},
      }),
    );

  test("an exportable row is ticked and can be changed", () => {
    const html = box({});
    const input = html.match(/<input[^>]*type="checkbox"[^>]*>/)![0];
    expect(input).toContain("checked");
    expect(input).not.toContain("disabled");
  });

  test("a row that is not exportable is not ticked, is off, and says why", () => {
    const html = box({ exportable: false });
    const input = html.match(/<input[^>]*type="checkbox"[^>]*>/)![0];
    expect(input).toContain("disabled");
    expect(input).not.toMatch(/checked/);
    expect(input).toContain('aria-describedby="pool-noexport-0"');
    expect(html).toMatch(
      /id="pool-noexport-0"[^>]*>Not exportable: left out of every export\./,
    );
  });
});
