import { afterEach, expect, test } from "bun:test";
import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
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
