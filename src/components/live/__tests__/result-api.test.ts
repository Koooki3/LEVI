import { describe, expect, test } from "bun:test";
import {
  annotationRunHref,
  resultEpisodeHref,
  canShowResult,
  resultDownloadHref,
  resultIndexPath,
  resultMatches,
  resultSessionPath,
} from "../result-api";
import { resultIdentity } from "../result-types";
import { parseSessionIdentity, identityHref } from "../session-route";
import { resultFixture } from "./result-fixture";
describe("precise result request identities", () => {
  test("keeps configured root, both session identities and literal percent/path/query names", () => {
    const item = resultFixture();
    const identity = {
      ...resultIdentity(item),
      root: "/root%/A?&",
      task: "task /%",
    };
    const path = resultSessionPath(identity)!;
    const q = new URL(path, "http://fixture").searchParams;
    expect(q.get("root")).toBe(identity.root);
    expect(q.get("task")).toBe(identity.task);
    expect(q.get("session")).toBe("run-1");
    expect(q.get("session_id")).toBe("session-1");
    expect(q.get("run_id")).toBe("run-1");
    expect(
      parseSessionIdentity(identityHref(identity).split("/sessions/")[1]),
    ).toEqual(identity);
  });
  test("does not substitute group/task analysis for an incomplete rootless bookmark", () => {
    const identity = { ...resultIdentity(resultFixture()), root: "" };
    expect(resultSessionPath(identity)).toBeNull();
    expect(resultDownloadHref(identity, "json", "zh")).toBeNull();
  });
  test("same session/run/task names from another root or rebound IDs fail the match", () => {
    const item = resultFixture();
    const identity = resultIdentity(item);
    for (const changed of [
      { root: "/other" },
      { id: "new-session" },
      { run: "new-run" },
      { dataset: "other" },
    ])
      expect(resultMatches(item, { ...identity, ...changed })).toBe(false);
    expect(resultMatches(item, identity)).toBe(true);
  });
  test("download uses only the explicit session identity, format and language", () => {
    const href = resultDownloadHref(
      resultIdentity(resultFixture()),
      "markdown",
      "zh",
    )!;
    const url = new URL(href, "http://fixture");
    expect(url.pathname).toBe("/api/levi/live/results/session/download");
    expect(url.searchParams.get("format")).toBe("markdown");
    expect(url.searchParams.get("lang")).toBe("zh");
    expect(url.searchParams.has("path")).toBe(false);
  });
  test("annotation run lookup and viewer deep link are independent from evaluation run", () => {
    expect(resultIndexPath("model__task", "annotation-1")).toBe(
      "/api/levi/live/results?dataset=model__task&run_id=annotation-1",
    );
    const item = resultFixture();
    expect(annotationRunHref(item.runs[0], 9)).toBe(
      "/local/model__task/episode_9?agent_run=annotation-1",
    );
    expect(resultEpisodeHref(item.viewers[0], 9)).toBe(
      "/local/live.model__task/episode_9?live_session=run-1",
    );
    expect(
      annotationRunHref({ ...item.runs[0], annotation_href: null }, 7),
    ).toBeNull();
    expect(
      annotationRunHref(
        {
          ...item.runs[0],
          annotation_href: "/local/model__task/episode_7?agent_run=run-1",
        },
        7,
      ),
    ).toBeNull();
  });
  test("nonvisible exact session status tolerates undisclosed datasets without unlocking results", () => {
    const item = resultFixture({
      status: "blocked",
      result_visibility: "withheld",
    });
    item.identity.datasets = [];
    expect(resultMatches(item, resultIdentity(resultFixture()))).toBe(true);
    expect(
      resultMatches(item, {
        ...resultIdentity(resultFixture()),
        root: "/wrong",
      }),
    ).toBe(false);
    expect(canShowResult(item)).toBe(false);
  });
  test("verified local viewer links keep alias while unsafe or unknown references stay plain", () => {
    const item = resultFixture();
    const viewer = item.viewers[0];
    expect(
      resultEpisodeHref(
        {
          ...viewer,
          viewer_href:
            "http://127.0.0.1:7880/local/native/episode_7?agent_run=a",
        },
        8,
      ),
    ).toBe("http://127.0.0.1:7880/local/native/episode_8?agent_run=a");
    for (const href of [
      "javascript:alert(1)",
      "https://outside.example/local/native/episode_7",
      "http://user:pass@localhost/local/native/episode_7",
      "/local/native/../episode_7",
      "/api/download?path=/tmp",
    ])
      expect(resultEpisodeHref({ ...viewer, viewer_href: href }, 7)).toBeNull();
    expect(resultEpisodeHref({ ...viewer, status: "unknown" }, 7)).toBeNull();
  });
  test("visibility is explicit: status alone never unlocks data", () => {
    expect(canShowResult(resultFixture())).toBe(true);
    for (const visibility of ["unknown", "withheld"] as const)
      expect(
        canShowResult(resultFixture({ result_visibility: visibility })),
      ).toBe(false);
    for (const status of [
      "active",
      "blocked",
      "archived",
      "rebound",
      "missing_source",
      "unknown",
    ] as const)
      expect(canShowResult(resultFixture({ status }))).toBe(false);
  });
});
