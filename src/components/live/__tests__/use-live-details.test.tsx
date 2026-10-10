import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { act } from "react";
import { useDatasetDetails } from "../use-live";
import type { DatasetRow } from "../types";

setupDom();
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});
const rows = Object.fromEntries(
  Array.from({ length: 5 }, (_, i) => [
    `dataset-${i}`,
    {
      episodes: 1,
      pending: 0,
      annotating: 0,
      done: 1,
      failed: 0,
    } satisfies DatasetRow,
  ]),
);
const names = Object.keys(rows);
function Details({ wanted, open }: { wanted: string[]; open: string[] }) {
  const { details } = useDatasetDetails(wanted, rows, new Set(open), true);
  return <output>{Object.keys(details).length}</output>;
}

describe("on-demand pipeline details", () => {
  test("closed rows never fetch details even if included in wanted", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (path: Parameters<typeof fetch>[0]) => {
      calls.push(String(path));
      return new Response(
        JSON.stringify({ enabled: true, name: "dataset-0", demos: [] }),
        { status: 200 },
      );
    }) as unknown as typeof fetch;
    const { rerender } = await render(
      <Details wanted={[names[0]]} open={[]} />,
    );
    expect(calls).toEqual([]);
    await rerender(<Details wanted={[names[0]]} open={[names[0]]} />);
    await waitFor(() => calls.length === 1);
    expect(calls[0]).toBe("/api/levi/live/datasets/dataset-0");
  });
  test("all expanded rows eventually load while at most three requests run", async () => {
    const pending: Array<() => void> = [];
    let requests = 0;
    let active = 0;
    let peak = 0;
    globalThis.fetch = (() => {
      requests++;
      active++;
      peak = Math.max(peak, active);
      return new Promise<Response>((resolve) =>
        pending.push(() => {
          active--;
          resolve(
            new Response(
              JSON.stringify({ enabled: true, name: "test", demos: [] }),
              { status: 200 },
            ),
          );
        }),
      );
    }) as unknown as typeof fetch;
    const { host } = await render(<Details wanted={names} open={names} />);
    expect(requests).toBe(3);
    await act(async () => pending.shift()!());
    await waitFor(() => requests === 4);
    expect(active).toBe(3);
    await act(async () => pending.shift()!());
    await waitFor(() => requests === 5);
    await act(async () => {
      for (const finish of pending.splice(0)) finish();
    });
    await waitFor(() => host.textContent === "5");
    expect(peak).toBe(3);
  });
});
