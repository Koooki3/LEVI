import { fire, focus, render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("@/components/hf-auth-button", () => ({ default: () => null }));
mock.module("@/components/home/open-dataset", () => ({
  OpenDataset: () => null,
}));
const { default: ExploreGrid } = await import("../explore-grid");

setupDom();
afterEach(() => document.documentElement.removeAttribute("data-motion"));

const sets = [
  { id: "lerobot/svla_so101_pickplace", videoUrl: "/a.mp4" },
  { id: "lerobot/aloha_static_coffee", videoUrl: "/b.mp4" },
];
const hrefs = (root: ParentNode) =>
  [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

describe("the source switch", () => {
  test("is two links, Examples and All Hub datasets, and the current one says so", async () => {
    const { host } = await render(
      <ExploreGrid datasets={sets} currentPage={1} totalPages={1} />,
    );
    const nav = host.querySelector("nav.pg-explore-source")!;
    expect(nav.getAttribute("aria-label")).toBeTruthy();
    const links = [...nav.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/explore",
      "/explore?catalog=all",
    ]);
    expect(links.map((a) => a.getAttribute("aria-current"))).toEqual([
      "page",
      null,
    ]);
    // never "Live": that word names the evaluation page and the report badge
    expect(nav.textContent).not.toMatch(/live/i);
    expect(host.querySelector(".pg-head-actions button.ds-btn")).toBeNull();
  });

  test("on ?catalog=all the second link is current and no control links to itself as an action", async () => {
    const { host } = await render(
      <ExploreGrid
        datasets={sets}
        currentPage={1}
        totalPages={1}
        catalog="all"
      />,
    );
    const links = [...host.querySelectorAll("nav.pg-explore-source a")];
    expect(links.map((a) => a.getAttribute("aria-current"))).toEqual([
      null,
      "page",
    ]);
  });
});

describe("paging", () => {
  test("a labelled nav of links, with page X of Y, and none for a single page", async () => {
    const { host } = await render(
      <ExploreGrid
        datasets={sets}
        currentPage={2}
        totalPages={4}
        catalog="all"
      />,
    );
    const pager = host.querySelector("nav.pg-explore-pager")!;
    expect(pager.getAttribute("aria-label")).toBeTruthy();
    expect(pager.querySelector("button")).toBeNull();
    expect(hrefs(pager)).toEqual([
      "/explore?catalog=all&p=1",
      "/explore?catalog=all&p=3",
    ]);
    expect(pager.textContent).toContain("Page 2 of 4");
    const single = await render(
      <ExploreGrid datasets={sets} currentPage={1} totalPages={1} />,
    );
    expect(single.host.querySelector("nav.pg-explore-pager")).toBeNull();
  });
});

describe("an error", () => {
  test("offers the curated list on the Hub view and keeps showing the examples", async () => {
    const { host } = await render(
      <ExploreGrid
        datasets={sets}
        currentPage={1}
        totalPages={1}
        catalog="all"
        error="Failed to load datasets."
      />,
    );
    const problem = host.textContent ?? "";
    expect(problem).toContain("Failed to load datasets.");
    expect(problem).toContain("Show the example datasets");
    expect(host.querySelectorAll(".pg-explore-card")).toHaveLength(2);
  });
});

describe("card previews", () => {
  const withVideo = async () => {
    const { host } = await render(
      <ExploreGrid datasets={sets} currentPage={1} totalPages={1} />,
    );
    const card = host.querySelector<HTMLAnchorElement>(".pg-explore-card")!;
    const video = card.querySelector("video")!;
    const calls: string[] = [];
    video.play = (() => {
      calls.push("play");
      return Promise.resolve();
    }) as typeof video.play;
    video.pause = (() => {
      calls.push("pause");
    }) as typeof video.pause;
    return { card, calls, host };
  };

  test("a card is named by its dataset id, and its preview plays on keyboard focus and stops on blur", async () => {
    const { card, calls } = await withVideo();
    expect(card.getAttribute("aria-label")).toBe(
      "lerobot/svla_so101_pickplace",
    );
    expect(card.querySelector("video")!.getAttribute("aria-hidden")).toBe(
      "true",
    );
    await focus(card);
    expect(calls).toEqual(["play"]);
    await fire(card, new FocusEvent("focusout", { bubbles: true }));
    expect(calls).toEqual(["play", "pause"]);
  });

  test("with reduced motion (the app setting) nothing starts by itself", async () => {
    document.documentElement.setAttribute("data-motion", "reduce");
    const { card, calls } = await withVideo();
    await act(async () => undefined);
    await focus(card);
    await fire(card, new MouseEvent("mouseover", { bubbles: true }));
    expect(calls).not.toContain("play");
  });
});
