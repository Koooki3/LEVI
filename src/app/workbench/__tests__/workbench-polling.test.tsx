import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/workbench",
}));
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));

const { default: Workbench } = await import("../page");

setupDom();

const realFetch = globalThis.fetch;
const realInterval = globalThis.setInterval;
afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.setInterval = realInterval;
  Object.defineProperty(document, "visibilityState", {
    value: "visible",
    configurable: true,
  });
});

/** The page polls every second while a job runs; the test makes it 20 ms. */
function pollFast() {
  globalThis.setInterval = ((fn: () => void, ms?: number) =>
    realInterval(fn, ms === 1000 ? 20 : ms)) as typeof setInterval;
}

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

const job = (id: string, status: string) => ({
  id,
  stage: "pipeline",
  source: "/s",
  output: "/o",
  argv: [],
  status,
});

/**
 * `held`: how many catalog reads are answered at all; later ones stay open
 * until the test ends (a slow service), so a second read in flight shows up
 * as an extra request.
 */
function serve(jobs: unknown[], held = Infinity) {
  const counts = { catalog: 0, jobs: 0 };
  globalThis.fetch = mock((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.includes("/convert/formats"))
      return Promise.resolve(
        json({ inputs: [], outputs: [], unsupported: [] }),
      );
    if (path.endsWith("/jobs")) {
      counts.jobs += 1;
      return Promise.resolve(json(jobs));
    }
    if (path.includes("/catalog")) {
      counts.catalog += 1;
      if (counts.catalog > held) return new Promise<Response>(() => {});
      return Promise.resolve(
        json({
          local: [],
          workspace: "/w",
          conversion_available: true,
          stages: [],
          linked: [],
        }),
      );
    }
    if (path.includes("/sync"))
      return Promise.resolve(
        json({
          enabled: true,
          running: true,
          interval_seconds: 5,
          last_scan: null,
          last_error: null,
          pending: [],
          changes: [],
        }),
      );
    return Promise.resolve(json([]));
  }) as unknown as typeof fetch;
  return counts;
}

describe("the job poll while a job runs", () => {
  test("never has two reads in flight", async () => {
    pollFast();
    // The first read answers (so the job shows as running and the poll goes
    // to every 20 ms here); every later read never answers.
    const counts = serve([job("20261105-1000-1", "running")], 1);
    await render(<Workbench />);
    await waitFor(() => counts.catalog >= 2, { label: "the second read" });
    await flush(300);
    expect(counts.catalog).toBe(2);
  });

  test("a hidden tab does not poll, and coming back reads at once", async () => {
    pollFast();
    const counts = serve([job("20261105-1000-1", "running")]);
    await render(<Workbench />);
    await waitFor(() => counts.catalog >= 2, { label: "a few polls" });
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    await flush(60);
    const hiddenAt = counts.catalog;
    await flush(200);
    expect(counts.catalog).toBe(hiddenAt);
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    });
    document.dispatchEvent(new Event("visibilitychange"));
    await waitFor(() => counts.catalog > hiddenAt, {
      label: "a read on return",
    });
  });
});

describe("task history order", () => {
  const rowsOf = (host: HTMLElement) =>
    [...host.querySelectorAll(".pg-history-item > summary code")].map(
      (n) => n.textContent,
    );

  test("an old failure sits among the successes by time, not above them", async () => {
    const jobs = [
      job("20260101-0000-1", "failed"),
      ...Array.from({ length: 9 }, (_, i) =>
        job(`2026110${i + 1}-1200-1`, "succeeded"),
      ),
    ];
    serve(jobs);
    const { host } = await render(<Workbench />);
    await waitFor(() => rowsOf(host).length > 0, { label: "the history" });
    const rows = rowsOf(host);
    // It still needs a look, so it is on the first page; but last, by time.
    expect(rows).toHaveLength(8);
    expect(rows[0]).toBe("20261109-1200-1");
    expect(rows.at(-1)).toBe("20260101-0000-1");
    expect(rows.slice(0, -1)).toEqual([...rows.slice(0, -1)].sort().reverse());
    const all = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Show all 10"),
    )!;
    await click(all);
    expect(rowsOf(host)).toHaveLength(10);
    expect(rowsOf(host).at(-1)).toBe("20260101-0000-1");
  });

  test("a failure newer than some successes is ordered between them", async () => {
    serve([
      job("20261101-1000-1", "succeeded"),
      job("20261103-1000-1", "failed"),
      job("20261102-1000-1", "succeeded"),
    ]);
    const { host } = await render(<Workbench />);
    await waitFor(() => rowsOf(host).length > 0, { label: "the history" });
    expect(rowsOf(host)).toEqual([
      "20261103-1000-1",
      "20261102-1000-1",
      "20261101-1000-1",
    ]);
  });

  test("a job that is still running stays on the first page however old", async () => {
    const jobs = [
      job("20260101-0000-1", "running"),
      ...Array.from({ length: 9 }, (_, i) =>
        job(`2026110${i + 1}-1200-1`, "succeeded"),
      ),
    ];
    serve(jobs);
    const { host } = await render(<Workbench />);
    await waitFor(() => rowsOf(host).length > 0, { label: "the history" });
    expect(rowsOf(host)).toContain("20260101-0000-1");
    expect(rowsOf(host)).toHaveLength(8);
  });
});
