import { afterEach, expect, test } from "bun:test";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { useLiveStats } from "../use-live-stats";
setupDom();
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
function Probe({
  dataset,
  enabled = true,
}: {
  dataset: string;
  enabled?: boolean;
}) {
  const request = useLiveStats(
    { dataset, session: "run-1", includeExcluded: false },
    10,
    null,
    false,
    enabled,
  );
  return (
    <div>
      {request.data?.episodes?.rows?.map((row) => row.demo).join(",") ||
        "empty"}
    </div>
  );
}
test("scope changes clear old rows while the new response is pending and reject late replies", async () => {
  let resolveB: (response: Response) => void = () => {};
  globalThis.fetch = ((path: unknown) =>
    String(path).includes("dataset=B")
      ? new Promise<Response>((r) => {
          resolveB = r;
        })
      : Promise.resolve(
          Response.json({
            enabled: true,
            episodes: { rows: [{ demo: "row-A" }], total: 1 },
          }),
        )) as typeof fetch;
  const { host, rerender } = await render(<Probe dataset="A" />);
  await waitFor(() => host.textContent === "row-A");
  await rerender(<Probe dataset="B" />);
  expect(host.textContent).toBe("empty");
  await rerender(<Probe dataset="A" enabled={false} />);
  resolveB(
    Response.json({
      enabled: true,
      episodes: { rows: [{ demo: "row-B" }], total: 1 },
    }),
  );
  await waitFor(() => host.textContent === "empty");
  await rerender(<Probe dataset="A" />);
  await waitFor(() => host.textContent === "row-A");
  expect(host.textContent).not.toContain("row-B");
});

function RefreshProbe({ dataset = "A" }: { dataset?: string }) {
  const stats = useLiveStats(
    { dataset, session: "run-1" },
    10,
    null,
    false,
    true,
  );
  return (
    <div>
      <button onClick={stats.refresh}>retry</button>
      <span>
        {stats.data?.episodes?.rows?.map((r) => r.demo).join(",") || "empty"}
      </span>
      <p>{stats.error}</p>
    </div>
  );
}
test("same-scope pending and failed refresh immediately revoke prior results", async () => {
  let pending = false;
  let resolve: (response: Response) => void = () => {};
  globalThis.fetch = (() =>
    pending
      ? new Promise<Response>((r) => {
          resolve = r;
        })
      : Promise.resolve(
          Response.json({
            episodes: { rows: [{ demo: "old-permitted-row" }] },
          }),
        )) as typeof fetch;
  const { host } = await render(<RefreshProbe />);
  await waitFor(() => host.textContent?.includes("old-permitted-row"));
  pending = true;
  await click(host.querySelector("button"));
  expect(host.textContent).not.toContain("old-permitted-row");
  resolve(new Response("source changed", { status: 409 }));
  await waitFor(() => host.textContent?.includes("HTTP 409"));
  expect(host.textContent).not.toContain("old-permitted-row");
});
test("an abandoned request error cannot clear a newer successful same-scope refresh", async () => {
  let rejectOld: (error: Error) => void = () => {};
  let count = 0;
  globalThis.fetch = (() =>
    ++count === 1
      ? new Promise<Response>((_, reject) => {
          rejectOld = reject;
        })
      : Promise.resolve(
          Response.json({ episodes: { rows: [{ demo: "new-current-row" }] } }),
        )) as typeof fetch;
  const { host } = await render(<RefreshProbe />);
  await click(host.querySelector("button"));
  await waitFor(() => host.textContent?.includes("new-current-row"));
  rejectOld(new Error("stale request error"));
  await new Promise((r) => setTimeout(r, 10));
  expect(host.textContent).toContain("new-current-row");
  expect(host.textContent).not.toContain("stale request error");
});
