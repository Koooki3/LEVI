import {
  render,
  setupDom,
  waitFor,
  flush,
} from "@/components/ds/__tests__/dom";
import { act } from "react";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/pool",
}));

const { default: PoolPage } = await import("../page");

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  Object.defineProperty(document, "visibilityState", {
    value: "visible",
    configurable: true,
  });
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const RUNNING_SCAN = { id: "scan-1", kind: "scan", status: "running" };
const STATUS = {
  enabled: true,
  roots: ["/pool"],
  export_roots: ["/exports"],
  heldout_lists: [],
  warnings: [],
  disk: [],
  jobs: [RUNNING_SCAN],
  last_scan: null,
};
const DOWN = () => json({ detail: "LEVI backend unavailable" }, 502);

/** Serve the page; `status(n)` answers the n-th status request (1-based). */
function serve(status: (n: number) => Response) {
  const calls = { status: 0 };
  globalThis.fetch = mock((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.includes("/pool/status")) {
      calls.status += 1;
      return Promise.resolve(status(calls.status));
    }
    if (path.includes("/pool/recipes"))
      return Promise.resolve(json({ recipes: [] }));
    if (path.includes("/pool/cleanup"))
      return Promise.resolve(
        json({ partials: [], jobs: [], disk: [], reclaimable_bytes: 0 }),
      );
    return Promise.resolve(json({}));
  }) as unknown as typeof fetch;
  return calls;
}

describe("the training pool's status poll", () => {
  test("the standing error goes away when a later read succeeds", async () => {
    serve((n) => (n === 1 ? DOWN() : json(STATUS)));
    const { host } = await render(<PoolPage />);
    await waitFor(() => host.querySelector(".pg-problem"), {
      label: "the read error",
    });
    await waitFor(() => !host.querySelector(".pg-problem"), {
      label: "the error cleared by the next successful read",
    });
  }, 15000);

  test("a failed poll is said in words next to the age, then cleared", async () => {
    // 1 = mount, 2 = first poll (fails), 3 = next poll.
    serve((n) => (n === 2 ? DOWN() : json(STATUS)));
    const { host } = await render(<PoolPage />);
    const line = () => host.querySelector("p[data-failures]");
    await waitFor(() => line()?.textContent?.includes("Updated"), {
      label: "the freshness line",
    });
    expect(line()!.textContent).toContain("refreshes every 1 s");
    await waitFor(() => line()?.textContent?.includes("last request failed"), {
      label: "the failed-poll note",
    });
    expect(line()!.textContent).toContain("retrying");
    // A blip on a page that has read the pool is not a standing error.
    expect(host.querySelector(".pg-problem")).toBeNull();
    await waitFor(() => !line()?.textContent?.includes("last request failed"), {
      label: "the note gone after a successful poll",
    });
  }, 15000);

  test("nothing is polled while the tab is hidden, and it is read again when shown", async () => {
    const calls = serve(() => json(STATUS));
    await render(<PoolPage />);
    await waitFor(() => calls.status >= 1, { label: "the first read" });
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    const before = calls.status;
    await flush(2600);
    expect(calls.status).toBe(before);
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => calls.status > before, { label: "a read on return" });
  }, 15000);
});
