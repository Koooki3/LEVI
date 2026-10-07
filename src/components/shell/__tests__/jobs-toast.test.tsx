import { render, setupDom } from "@/components/ds/__tests__/dom";
import { act } from "react";
import { describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/",
}));

const { ToastProvider } = await import("@/components/ds");
const { JobsMenu } = await import("../jobs-menu");
const {
  conversionEntries,
  finishedSince,
  jobOutcome,
  poolEntries,
  runningFraction,
  JOBS_ACTIVE_POLL_MS,
  JOBS_POLL_MS,
} = await import("../jobs");

setupDom();

const pool = (jobs: unknown[]) => ({ jobs });

describe("job entries", () => {
  test("pool and conversion rows become entries with progress", () => {
    const entries = [
      ...poolEntries(
        pool([
          {
            id: "p1",
            kind: "export",
            status: "running",
            options: { name: "plates-v3" },
            progress: { done: 25, total: 100 },
          },
          { id: "p2", kind: "scan", status: "done" },
          { status: "running" },
        ]),
      ),
      ...conversionEntries([
        {
          id: "c1",
          status: "running",
          dataset: "screws",
          progress: { done: 1, total: 4 },
        },
        { id: "c2", status: "failed", source: "/data/task_a/" },
      ]),
    ];
    expect(entries.map((e) => e.key)).toEqual([
      "pool:p1",
      "pool:p2",
      "conversion:c1",
      "conversion:c2",
    ]);
    expect(entries[0].name).toBe("plates-v3");
    expect(entries[0].what).toBe("Training pool export");
    expect(entries[3].name).toBe("task_a");
    expect(runningFraction(entries, "pool")).toBeCloseTo(0.25);
    expect(runningFraction(entries, "conversion")).toBeCloseTo(0.25);
    expect(runningFraction([], "pool")).toBeNull();
  });

  test("a result is announced once, for a job seen running, never for old ones", () => {
    const running = poolEntries(pool([{ id: "a", status: "running" }]));
    const map = new Map(running.map((e) => [e.key, e]));
    const done = poolEntries(pool([{ id: "a", status: "done" }]));
    expect(finishedSince(null, done)).toEqual([]);
    expect(finishedSince(map, done).map((f) => f.outcome)).toEqual(["success"]);
    expect(
      finishedSince(
        map,
        poolEntries(pool([{ id: "a", status: "failed" }])),
      ).map((f) => f.outcome),
    ).toEqual(["failure"]);
    expect(
      finishedSince(
        map,
        poolEntries(pool([{ id: "a", status: "done_with_errors" }])),
      ).map((f) => f.outcome),
    ).toEqual(["warning"]);
    expect(
      finishedSince(
        map,
        poolEntries(pool([{ id: "a", status: "done_with_warnings" }])),
      ).map((f) => f.outcome),
    ).toEqual(["warning"]);
    // Cancelled or interrupted: no toast. Still running: none. Gone: none.
    for (const status of ["cancelled", "interrupted", "running"])
      expect(
        finishedSince(map, poolEntries(pool([{ id: "a", status }]))),
      ).toEqual([]);
    expect(finishedSince(map, [])).toEqual([]);
    expect(
      jobOutcome(conversionEntries([{ id: "c", status: "succeeded" }])[0]),
    ).toBe("success");
  });

  test("a running job is asked about more often than an idle entry", () => {
    expect(JOBS_ACTIVE_POLL_MS).toBeLessThan(JOBS_POLL_MS);
  });
});

describe("the Jobs entry", () => {
  test("shows a running job's progress, then says when it has finished", async () => {
    let phase: "running" | "done" = "running";
    const original = globalThis.fetch;
    globalThis.fetch = ((url: string) => {
      const body = String(url).includes("pool")
        ? {
            jobs: [
              phase === "running"
                ? {
                    id: "p1",
                    kind: "export",
                    status: "running",
                    options: { name: "plates-v3" },
                    progress: { done: 3, total: 4 },
                  }
                : {
                    id: "p1",
                    kind: "export",
                    status: "done",
                    options: { name: "plates-v3" },
                  },
            ],
          }
        : [];
      return Promise.resolve(new Response(JSON.stringify(body)));
    }) as unknown as typeof fetch;
    const realSetInterval = globalThis.setInterval;
    const ticks: Array<() => void> = [];
    globalThis.setInterval = ((fn: () => void) => {
      ticks.push(fn);
      return ticks.length as unknown as ReturnType<typeof setInterval>;
    }) as unknown as typeof setInterval;
    try {
      const { host } = await render(
        <ToastProvider>
          <JobsMenu pool />
        </ToastProvider>,
      );
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
      const trigger = host.querySelector<HTMLButtonElement>(
        'button[aria-haspopup="menu"]',
      )!;
      expect(
        trigger.querySelector(".ds-menu-trigger__badge")?.textContent,
      ).toBe("1");
      await act(async () => trigger.click());
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
      const items = [...document.querySelectorAll('[role="menuitem"]')].map(
        (e) => e.textContent,
      );
      expect(items.join("|")).toContain("1 running · 75%");
      await act(async () => trigger.click());
      // The job finishes; the next poll announces it.
      phase = "done";
      await act(async () => {
        ticks.forEach((tick) => tick());
        await new Promise((r) => setTimeout(r, 0));
      });
      const region = document.body.textContent ?? "";
      expect(region).toContain("Training pool export: finished");
      expect(region).toContain("plates-v3");
      expect(region).toContain("Show the job");
    } finally {
      globalThis.fetch = original;
      globalThis.setInterval = realSetInterval;
    }
  });

  test("in Chinese the title uses the full-width colon", async () => {
    const { LocaleProvider } = await import("@/components/levi-locale");
    window.localStorage.setItem("levi-language", "zh");
    let phase: "running" | "done" = "running";
    const original = globalThis.fetch;
    globalThis.fetch = ((url: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            String(url).includes("pool")
              ? {
                  jobs: [
                    {
                      id: "p1",
                      kind: "export",
                      status: phase,
                      options: { name: "plates-v3" },
                    },
                  ],
                }
              : [],
          ),
        ),
      )) as unknown as typeof fetch;
    const realSetInterval = globalThis.setInterval;
    const ticks: Array<() => void> = [];
    globalThis.setInterval = ((fn: () => void) => {
      ticks.push(fn);
      return ticks.length as unknown as ReturnType<typeof setInterval>;
    }) as unknown as typeof setInterval;
    try {
      await render(
        <LocaleProvider>
          <ToastProvider>
            <JobsMenu pool />
          </ToastProvider>
        </LocaleProvider>,
      );
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
      phase = "done";
      await act(async () => {
        ticks.forEach((tick) => tick());
        await new Promise((r) => setTimeout(r, 0));
      });
      const region = document.body.textContent ?? "";
      expect(region).toContain("训练池导出：已完成");
      expect(region).not.toContain("训练池导出: ");
    } finally {
      globalThis.fetch = original;
      globalThis.setInterval = realSetInterval;
      window.localStorage.removeItem("levi-language");
    }
  });

  test("when the service does not answer the menu says so, never 'no job is running', and offers a retry", async () => {
    let answering = false;
    const original = globalThis.fetch;
    const asked: string[] = [];
    globalThis.fetch = ((url: string) => {
      asked.push(String(url));
      return Promise.resolve(
        answering
          ? new Response(
              JSON.stringify(String(url).includes("pool") ? { jobs: [] } : []),
            )
          : new Response("{}", { status: 502 }),
      );
    }) as unknown as typeof fetch;
    try {
      const { host } = await render(
        <ToastProvider>
          <JobsMenu pool />
        </ToastProvider>,
      );
      const trigger = host.querySelector<HTMLButtonElement>(
        'button[aria-haspopup="menu"]',
      )!;
      await act(async () => trigger.click());
      await act(async () => {
        await new Promise((r) => setTimeout(r, 20));
      });
      const items = () =>
        [...document.querySelectorAll('[role="menuitem"]')].map(
          (e) => e.textContent ?? "",
        );
      expect(items().join("|")).toContain("could not be checked just now");
      expect(items().join("|")).not.toContain("no job is running");
      expect(items().some((text) => text.includes("Try again"))).toBe(true);
      // The service is back: a retry reads it and says there is none.
      answering = true;
      const retry = [...document.querySelectorAll('[role="menuitem"]')].find(
        (e) => e.textContent?.includes("Try again"),
      ) as HTMLElement;
      await act(async () => retry.click());
      await act(async () => {
        await new Promise((r) => setTimeout(r, 20));
      });
      await act(async () => trigger.click());
      await act(async () => {
        await new Promise((r) => setTimeout(r, 20));
      });
      expect(items().join("|")).toContain("no job is running");
      expect(asked.length).toBeGreaterThan(2);
    } finally {
      globalThis.fetch = original;
    }
  });
});
