import { afterEach, describe, expect, mock, test } from "bun:test";
import {
  render,
  setupDom,
  waitFor,
  click,
  fire,
} from "@/components/ds/__tests__/dom";
import { resultFixture } from "./result-fixture";
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { ResultDirectory, matchingResults } =
  await import("../result-directory");
const { AnnotationSessionLinks } = await import("../annotation-session-links");
setupDom();
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
const index = (items: ReturnType<typeof resultFixture>[]) => ({
  schema: "levi.live.result-index.v1",
  available: true,
  items,
});
describe("result source index", () => {
  test("same task and session names from two roots remain two exact links", async () => {
    const first = resultFixture();
    const second = resultFixture({
      identity: { ...first.identity, root: "/other-root" },
    });
    globalThis.fetch = (async () =>
      Response.json(index([first, second]))) as typeof fetch;
    const { host } = await render(<ResultDirectory />);
    await waitFor(() => host.querySelectorAll("article").length === 2);
    const links = [...host.querySelectorAll("article h3 a")].map((a) =>
      a.getAttribute("href"),
    );
    expect(new Set(links).size).toBe(2);
    expect(host.textContent).toContain("/fixture");
    expect(host.textContent).toContain("/other-root");
  });
  test("source list remains paged and search reduces model/task/version without auto grouping", async () => {
    const items = Array.from({ length: 8 }, (_, i) => {
      const item = resultFixture();
      return {
        ...item,
        identity: { ...item.identity, root: `/root-${i}` },
        policy: {
          ...item.policy,
          checkpoint: i === 7 ? "wanted-model" : "other",
        },
      };
    });
    globalThis.fetch = (async () =>
      Response.json(index(items))) as typeof fetch;
    const { host } = await render(<ResultDirectory />);
    await waitFor(() => host.querySelectorAll("article").length === 6);
    await click(
      [...host.querySelectorAll("button")].find(
        (b) => b.textContent === "Next",
      )!,
    );
    expect(host.querySelectorAll("article")).toHaveLength(2);
    const input = host.querySelector("input")!;
    input.value = "wanted-model";
    await fire(input, new Event("input", { bubbles: true }));
    // The pure query and the paging control both preserve exact identities.
    expect(matchingResults(items, "wanted-model report-v1")).toHaveLength(1);
  });
  test("truncated scans and unverified historical versions are explicit and searchable without downloads", async () => {
    const item = resultFixture({
      status: "archived",
      result_visibility: "unknown",
      source: {
        kind: "catalog_reference",
        snapshot: null,
        basis: "operator_and_automatic_separate",
        missing: [],
        catalog_versions: [
          { basis: "operator", version: "history-v9", status: "unverified" },
        ],
      },
    });
    globalThis.fetch = (async () =>
      Response.json({
        ...index([item]),
        complete: false,
        truncated: ["catalog"],
        limits: { catalog_entries: 100 },
      })) as typeof fetch;
    const { host } = await render(<ResultDirectory />);
    await waitFor(() => host.textContent?.includes("This index is incomplete"));
    expect(host.textContent).toContain("history-v9");
    expect(matchingResults([item], "history-v9")).toHaveLength(1);
    expect(matchingResults([item], "report-v1")).toHaveLength(0);
    expect(host.querySelector('a[href*="download"]') === null).toBe(true);
  });
  test("backlink requires annotation dataset/run intersection, never evaluation run or first row", async () => {
    const good = resultFixture();
    const wrong = resultFixture({
      identity: { ...good.identity, root: "/wrong" },
      viewers: [],
      runs: [
        {
          dataset: "other-dataset",
          kind: "temporal",
          run_id: "annotation-1",
          changeset: null,
          repo_id: "local/other-dataset",
          viewer_href: null,
          annotation_href: null,
          episodes: [7],
        },
      ],
    });
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      return Response.json(index([good, wrong]));
    }) as typeof fetch;
    const { host } = await render(
      <AnnotationSessionLinks repo="local/model__task" run="annotation-1" />,
    );
    await waitFor(() => host.querySelector("a") !== null);
    expect(calls[0]).toContain("dataset=model__task&run_id=annotation-1");
    expect(host.querySelectorAll("a")).toHaveLength(1);
    expect(host.textContent).not.toContain("/wrong");
    expect(host.textContent).toContain("run-1");
  });
  test("ambiguous verified links are all listed rather than selecting a root", async () => {
    const first = resultFixture();
    const second = resultFixture({
      identity: { ...first.identity, root: "/second" },
    });
    globalThis.fetch = (async () =>
      Response.json(index([first, second]))) as typeof fetch;
    const { host } = await render(
      <AnnotationSessionLinks repo="local/model__task" run="annotation-1" />,
    );
    await waitFor(() => host.querySelectorAll("a").length === 2);
    expect(host.textContent).toContain("/second");
  });
  test("remote datasets never trigger guessed live source lookup", async () => {
    let calls = 0;
    globalThis.fetch = (async () => {
      calls++;
      return Response.json(index([]));
    }) as typeof fetch;
    const { host } = await render(
      <AnnotationSessionLinks repo="remote/model__task" run="run-1" />,
    );
    expect(calls).toBe(0);
    expect(host.textContent).toBe("");
  });
});
