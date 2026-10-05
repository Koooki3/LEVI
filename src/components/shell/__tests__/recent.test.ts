import { describe, expect, test } from "bun:test";
import {
  addVisit,
  parseRecent,
  RECENT_LIMIT,
  visitFromPath,
  visitHref,
} from "../recent";

describe("recent visits", () => {
  test("only dataset and episode pages count", () => {
    expect(visitFromPath("/local/screws/episode_14")).toEqual({
      repo: "local/screws",
      episode: 14,
    });
    expect(visitFromPath("/lerobot/aloha")).toEqual({
      repo: "lerobot/aloha",
      episode: null,
    });
    for (const path of [
      "/",
      "/pool",
      "/explore/x",
      "/live/session",
      "/design",
      "/local/screws/stats",
      "/a/b/episode_x",
      "/a/b/episode_1/more",
      "/api/levi/catalog",
    ])
      expect(visitFromPath(path)).toBeNull();
  });

  test("newest first, one per dataset, bounded", () => {
    let list = addVisit([], { repo: "a/b", episode: 3 }, 1);
    list = addVisit(list, { repo: "c/d", episode: null }, 2);
    // Opening the dataset page again keeps the last episode seen.
    list = addVisit(list, { repo: "a/b", episode: null }, 3);
    expect(list).toEqual([
      { repo: "a/b", episode: 3, at: 3 },
      { repo: "c/d", episode: null, at: 2 },
    ]);
    for (let i = 0; i < 20; i++)
      list = addVisit(list, { repo: `o/d${i}`, episode: i }, 10 + i);
    expect(list).toHaveLength(RECENT_LIMIT);
    expect(list[0].repo).toBe("o/d19");
  });

  test("stored values are checked", () => {
    expect(parseRecent(null)).toEqual([]);
    expect(parseRecent("not json")).toEqual([]);
    expect(
      parseRecent(
        JSON.stringify([
          { repo: "a/b", episode: 1, at: 1 },
          { repo: "../x", episode: 1, at: 1 },
          { repo: "a/c", episode: -1, at: 1 },
          { repo: "a/d", episode: null, at: "x" },
        ]),
      ),
    ).toEqual([{ repo: "a/b", episode: 1, at: 1 }]);
    expect(visitHref({ repo: "a/b", episode: 2 })).toBe("/a/b/episode_2");
    expect(visitHref({ repo: "a/b", episode: null })).toBe("/a/b");
  });
});
