import { afterEach, describe, expect, mock, test } from "bun:test";
import {
  render,
  setupDom,
  waitFor,
  click,
} from "@/components/ds/__tests__/dom";
import { resultIdentity } from "../result-types";
import { resultFixture } from "./result-fixture";
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { SessionResults, ResultContent } = await import("../session-results");
setupDom();
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
describe("exact session results", () => {
  test("keeps label sources separate, accessible measured funnel, snapshot and fixed report version", async () => {
    const item = resultFixture();
    const { host } = await render(
      <ResultContent item={item} identity={resultIdentity(item)} />,
    );
    expect(host.textContent).toContain("Success rate (operator labels)");
    expect(host.textContent).toContain("Success rate (automatic verdicts)");
    expect(host.querySelector('meter[aria-label="grasp: 50%"]')).not.toBeNull();
    expect(host.textContent).toContain("report-v1");
    expect(host.textContent).toContain("Stale report");
    expect(host.textContent).toContain("snapshot-hash");
    expect(
      host.querySelector('a[href*="agent_run=annotation-1"]'),
    ).not.toBeNull();
    expect(
      host.querySelector('a[href*="agent_run=annotation-2"]'),
    ).not.toBeNull();
    expect(host.querySelector('a[href*="agent_run=run-1"]') === null).toBe(
      true,
    );
    expect(host.querySelectorAll('a[href*="/download?"]')).toHaveLength(2);
    expect(host.textContent).toContain("Automatic, unreviewed");
    expect(host.textContent).toContain("release-rule");
    expect(host.textContent).toContain("generic-v2");
    expect(host.textContent).toContain("v3");
  });
  test("withheld and unknown visibility hide even accidentally populated analysis/records/reports", async () => {
    for (const visibility of ["withheld", "unknown"] as const) {
      const item = resultFixture({ result_visibility: visibility });
      const { host } = await render(
        <ResultContent item={item} identity={resultIdentity(item)} />,
      );
      expect(host.querySelector("meter") === null).toBe(true);
      expect(host.textContent).not.toContain("demo-7");
      expect(host.textContent).not.toContain("report-v1");
      expect(host.querySelector('a[href*="/download?"]') === null).toBe(true);
    }
  });
  test("exact root and IDs travel to the real API, wrong-root response does not render figures", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: unknown) => {
      calls.push(String(path));
      const item = resultFixture();
      return Response.json({
        ...item,
        identity: { ...item.identity, root: "/wrong" },
      });
    }) as typeof fetch;
    const { host } = await render(
      <SessionResults identity={resultIdentity(resultFixture())} />,
    );
    await waitFor(() => host.textContent?.includes("Session source changed."));
    expect(calls[0]).toContain("root=%2Ffixture");
    expect(calls[0]).toContain("session_id=session-1");
    expect(host.querySelector("meter") === null).toBe(true);
  });
  test("a new bookmark clears old content immediately and ignores an out-of-order answer", async () => {
    const first = resultFixture();
    const second = resultFixture({
      identity: { ...first.identity, root: "/second" },
    });
    let resolveFirst: (response: Response) => void = () => {};
    globalThis.fetch = ((path: unknown) =>
      String(path).includes("root=%2Ffixture")
        ? new Promise<Response>((r) => {
            resolveFirst = r;
          })
        : Promise.resolve(Response.json(second))) as typeof fetch;
    const { host, rerender } = await render(
      <SessionResults identity={resultIdentity(first)} />,
    );
    await rerender(<SessionResults identity={resultIdentity(second)} />);
    await waitFor(() => host.textContent?.includes("/second"));
    resolveFirst(Response.json(first));
    await new Promise((r) => setTimeout(r, 5));
    expect(host.textContent).not.toContain("/fixture");
  });
  test("failed refresh removes prior snapshot and download links until a successful retry", async () => {
    let available = true;
    globalThis.fetch = (async () =>
      available
        ? Response.json(resultFixture())
        : new Response("blocked", { status: 409 })) as typeof fetch;
    const { host, rerender } = await render(
      <SessionResults
        identity={resultIdentity(resultFixture())}
        tick={30000}
      />,
    );
    await waitFor(() => host.querySelector("meter") !== null);
    available = false;
    await rerender(
      <SessionResults
        identity={resultIdentity(resultFixture())}
        tick={60000}
      />,
    );
    await waitFor(() => host.textContent?.includes("HTTP 409"));
    expect(host.querySelector("meter") === null).toBe(true);
    expect(host.querySelector('a[href*="/download?"]') === null).toBe(true);
    available = true;
    await click(
      [...host.querySelectorAll("button")].find(
        (b) => b.textContent === "Refresh",
      )!,
    );
    await waitFor(() => host.querySelector("meter") !== null);
  });
  test("waiting, partial and stale records retain explicit status without inventing missing analysis", async () => {
    for (const status of ["waiting_annotation", "partial", "stale"] as const) {
      const item = resultFixture({
        status,
        analysis: null,
        records: [],
        runs: [],
        reports: [],
      });
      const { host } = await render(
        <ResultContent item={item} identity={resultIdentity(item)} />,
      );
      expect(host.textContent).toContain(
        "No analysis records are available for this exact session yet",
      );
      expect(host.querySelector("meter") === null).toBe(true);
      expect(host.querySelector('a[href*="download"]') === null).toBe(true);
      expect(host.textContent).not.toContain("0%");
    }
  });
  test("an operator-only analysis describes absent segments without an empty chart", async () => {
    const item = resultFixture();
    item.analysis!.all.funnel = { of: 0, stages: [] };
    const { host } = await render(
      <ResultContent item={item} identity={resultIdentity(item)} />,
    );
    expect(host.textContent).toContain(
      "No time segment annotations are available",
    );
    expect(host.querySelector("figure") === null).toBe(true);
  });
  test("historical versions remain unverified references with no figures or downloads", async () => {
    const item = resultFixture({
      status: "archived",
      result_visibility: "unknown",
      source: {
        kind: "catalog_reference",
        snapshot: null,
        basis: "operator_and_automatic_separate",
        missing: [],
        catalog_versions: [
          { basis: "operator", version: "historical-v3", status: "unverified" },
        ],
      },
    });
    const { host } = await render(
      <ResultContent item={item} identity={resultIdentity(item)} />,
    );
    expect(host.textContent).toContain("historical-v3");
    expect(host.textContent).toContain("Unverified source versions");
    expect(host.textContent).not.toContain("report-v1");
    expect(host.querySelector("meter") === null).toBe(true);
    expect(host.querySelector('a[href*="download"]') === null).toBe(true);
  });
  test("unknown comparison association lists missing conditions without ranking or guessing campaign", async () => {
    const item = resultFixture();
    const { host } = await render(
      <ResultContent item={item} identity={resultIdentity(item)} />,
    );
    expect(host.textContent).toContain("Comparison association is unknown");
    expect(host.textContent).toContain("Explicit campaign association");
    expect(host.textContent).toContain("Reset conditions");
    expect(host.textContent).toContain("Policies are not ranked here");
  });
});
