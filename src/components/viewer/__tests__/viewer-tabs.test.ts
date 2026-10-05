import { describe, expect, test } from "bun:test";
import {
  adjacentEpisode,
  loadsFor,
  restoreViewerTab,
  showsEpisodeList,
} from "../viewer-tabs";

describe("episode viewer tabs", () => {
  test("old analysis tab ids open the Analysis tab on that view", () => {
    expect(restoreViewerTab("insights", null)).toEqual({
      tab: "analysis",
      view: "insights",
    });
    expect(restoreViewerTab("filtering", "insights")).toEqual({
      tab: "analysis",
      view: "filtering",
    });
    expect(restoreViewerTab("doctor", null)).toEqual({
      tab: "analysis",
      view: "doctor",
    });
  });

  test("current tab ids and the stored view are kept; anything else opens Episodes", () => {
    expect(restoreViewerTab("analysis", "doctor")).toEqual({
      tab: "analysis",
      view: "doctor",
    });
    expect(restoreViewerTab("annotations", "filtering")).toEqual({
      tab: "annotations",
      view: "filtering",
    });
    expect(restoreViewerTab("frames", "bogus")).toEqual({
      tab: "frames",
      view: "insights",
    });
    expect(restoreViewerTab(null, null)).toEqual({
      tab: "episodes",
      view: "insights",
    });
    expect(restoreViewerTab("nope", null).tab).toBe("episodes");
  });

  test("each view loads what its former tab loaded", () => {
    const none = { stats: false, frames: false, insights: false };
    expect(loadsFor("episodes", "insights")).toEqual(none);
    expect(loadsFor("statistics", "insights")).toEqual({
      ...none,
      stats: true,
    });
    expect(loadsFor("frames", "doctor")).toEqual({ ...none, frames: true });
    expect(loadsFor("analysis", "insights")).toEqual({
      ...none,
      insights: true,
    });
    expect(loadsFor("analysis", "filtering")).toEqual({
      ...none,
      stats: true,
      insights: true,
    });
    expect(loadsFor("analysis", "doctor")).toEqual(none);
  });

  test("the episode list shows beside episodes, annotations and 3D replay only", () => {
    expect(showsEpisodeList("episodes")).toBe(true);
    expect(showsEpisodeList("annotations")).toBe(true);
    expect(showsEpisodeList("urdf")).toBe(true);
    expect(showsEpisodeList("analysis")).toBe(false);
    expect(showsEpisodeList("statistics")).toBe(false);
    expect(showsEpisodeList("frames")).toBe(false);
  });

  test("previous / next episode walk the visible list and stop at the ends", () => {
    const list = [2, 5, 9];
    expect(adjacentEpisode(list, 5, 1)).toBe(9);
    expect(adjacentEpisode(list, 5, -1)).toBe(2);
    expect(adjacentEpisode(list, 9, 1)).toBeUndefined();
    expect(adjacentEpisode(list, 2, -1)).toBeUndefined();
    // From an episode outside the (filtered) list: first or last.
    expect(adjacentEpisode(list, 7, 1)).toBe(2);
    expect(adjacentEpisode(list, 7, -1)).toBe(9);
    expect(adjacentEpisode([], 1, 1)).toBeUndefined();
  });
});
