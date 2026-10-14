import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { act } from "react";

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
afterEach(() => {
  globalThis.fetch = realFetch;
});

const json = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

const own = (i: number) => ({
  id: `local/own${i}`,
  name: `own${i}`,
  path: `/w/datasets/own${i}`,
  kind: "lerobot",
});
const linked = (i: number) => ({
  id: `local/live.run${i}`,
  name: `live.run${i}`,
  path: `/live-ws/run${i}`,
  kind: "raw",
  view_status: "ready",
  linked: {
    kind: "live",
    source: `run${i}`,
    workspace: "levi-live-ws",
    readonly: true,
  },
});

function serve(local: unknown[], jobList: unknown[] = []) {
  globalThis.fetch = mock((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.includes("/jobs")) return Promise.resolve(json(jobList));
    if (path.includes("/convert/formats"))
      return Promise.resolve(
        json({ inputs: [], outputs: [], unsupported: [] }),
      );
    if (path.includes("/catalog"))
      return Promise.resolve(
        json({
          local,
          workspace: "/w",
          conversion_available: true,
          stages: [],
          linked: [],
        }),
      );
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
}

const rows = (host: HTMLElement) => host.querySelectorAll("tbody tr");
const names = (host: HTMLElement) =>
  [...rows(host)].map((r) => r.querySelector("td")?.textContent ?? "");

// This DOM reads a controlled field's value on keyup (as in shell-dom.test).
async function typeInto(input: HTMLInputElement, value: string) {
  await act(async () => input.focus());
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
  await flush(20);
}

describe("the Local datasets list", () => {
  test("a person's own datasets come first, and a long list shows one page with a way to see all", async () => {
    serve([
      ...Array.from({ length: 14 }, (_, i) => linked(i)),
      ...Array.from({ length: 3 }, (_, i) => own(i)),
    ]);
    const { host } = await render(<Workbench />);
    await waitFor(() => (rows(host).length ? true : false));
    expect(rows(host).length).toBe(12);
    expect(
      names(host)
        .slice(0, 3)
        .map((n) => n.startsWith("own")),
    ).toEqual([true, true, true]);
    expect(host.textContent).toContain(
      "3 in this workspace · 14 read-only from live evaluation",
    );
    expect(host.textContent).toContain("Showing 12 of 17");
    const all = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Show all 17 datasets"),
    )!;
    await click(all);
    expect(rows(host).length).toBe(17);
    const fewer = [...host.querySelectorAll("button")].find(
      (b) => b.textContent === "Show fewer",
    )!;
    await click(fewer);
    expect(rows(host).length).toBe(12);
  });

  test("typing in the filter narrows the whole list, not just the page", async () => {
    serve([...Array.from({ length: 14 }, (_, i) => linked(i)), own(1)]);
    const { host } = await render(<Workbench />);
    await waitFor(() => (rows(host).length ? true : false));
    const input = host.querySelector(
      'input[type="search"]',
    ) as HTMLInputElement;
    await typeInto(input, "run13");
    expect(names(host).length).toBe(1);
    expect(names(host)[0]).toContain("live.run13");
    await typeInto(input, "nothing-like-this");
    expect(rows(host).length).toBe(0);
    expect(host.textContent).toContain(
      "No dataset matches “nothing-like-this”.",
    );
    // no paging buttons while a filter is typed
    expect(host.textContent).not.toContain("Show all");
  });

  test("a filter left in the box does not keep hiding rows once the box is gone", async () => {
    serve([...Array.from({ length: 7 }, (_, i) => own(i))]);
    const { host } = await render(<Workbench />);
    await waitFor(() => (rows(host).length ? true : false));
    await typeInto(
      host.querySelector('input[type="search"]') as HTMLInputElement,
      "own3",
    );
    expect(names(host)).toHaveLength(1);
    // the list drops to six: the box disappears, every dataset is back
    serve([...Array.from({ length: 6 }, (_, i) => own(i))]);
    await waitFor(() =>
      host.querySelector('input[type="search"]') ? false : true,
    );
    expect(rows(host)).toHaveLength(6);
    expect(host.textContent).not.toContain("No dataset matches");
  });

  test("a short list has no filter box and no paging", async () => {
    serve([own(1), own(2)]);
    const { host } = await render(<Workbench />);
    await waitFor(() => (rows(host).length ? true : false));
    expect(host.querySelector('input[type="search"]')).toBeNull();
    expect(host.textContent).not.toContain("Show all");
  });

  test("the task history shows the newest page with totals by outcome and a way to see all", async () => {
    const job = (i: number, status: string) => ({
      id: `20261014-${String(i).padStart(4, "0")}`,
      status,
      stage: "Build browsing view",
      log: "",
      output: "",
      progress: null,
    });
    const jobList = [
      ...Array.from({ length: 11 }, (_, i) => job(i, "succeeded")),
      job(11, "failed"),
      job(12, "interrupted"),
    ];
    serve([own(1)], jobList);
    const { host } = await render(<Workbench />);
    await waitFor(() => (rows(host).length ? true : false));
    const items = () => host.querySelectorAll("details.pg-history-item");
    await waitFor(() => (items().length ? true : false));
    expect(items().length).toBe(8);
    // the failed and the interrupted one are on the first page although they
    // are the oldest in the list
    expect(
      host.querySelector("details.pg-history-item")!.parentElement!.textContent,
    ).toContain("failed");
    expect(
      [...items()].filter((d) =>
        /failed|interrupted/.test(d.textContent ?? ""),
      ),
    ).toHaveLength(2);
    expect(host.textContent).toContain(
      "13 tasks: 11 succeeded · 1 failed · 1 interrupted",
    );
    const all = [...host.querySelectorAll("button")].find((b) =>
      b.textContent?.includes("Show all 13 tasks"),
    )!;
    await click(all);
    expect(items().length).toBe(13);
  });
});
