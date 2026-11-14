import { fire, flush, render, setupDom, waitFor } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { EpisodeTable } from "../pool-tables";
import { PickedList } from "../task-pick";
import type { EpisodeRow, PickedEpisodes } from "../types";

setupDom();

const row = {
  key: "a/demo_0001",
  source: "a",
  source_path: "/data/a",
  category: "human",
  task: "t",
  episode: "demo_0001",
  canonical: true,
  exportable: true,
  frames: 10,
  format: "lerobot",
  viewer: "/org/ds/episode_1",
} as unknown as EpisodeRow;

describe("episode links leave the page in a new tab", () => {
  test("the episode table's viewer link", () => {
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
    const link = html.match(/<a [^>]*href="\/org\/ds\/episode_1"[^>]*>/)![0];
    expect(link).toContain('target="_blank"');
    expect(link).toContain('rel="noreferrer"');
    // The cue is a named icon, not colour or a hover tooltip.
    expect(html).toContain('aria-label="opens in a new tab"');
  });

  test("the picked-episode list's viewer link", async () => {
    const picked = {
      episodes: [
        {
          key: "a/demo_0001",
          episode: "/data/a/demo_0001",
          viewer: "/org/ds/episode_1",
          quality_score: 0.5,
          selection_reason: [],
          frames: 3,
        },
      ],
    } as unknown as PickedEpisodes;
    const { host } = await render(
      <PickedList task="t" refreshKey="k" load={async () => picked} />,
    );
    const details = host.querySelector("details")!;
    details.open = true;
    await fire(details, new Event("toggle"));
    // The list is fetched after a 300 ms debounce.
    await flush(350);
    const link = await waitFor(
      () =>
        host.querySelector<HTMLAnchorElement>("a[href='/org/ds/episode_1']"),
      { label: "the picked episode link" },
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noreferrer");
    expect(
      link.querySelector("[aria-label='opens in a new tab']"),
    ).not.toBeNull();
  });
});
