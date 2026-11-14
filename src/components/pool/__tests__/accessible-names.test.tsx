import { render, setupDom, waitFor } from "../../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/pool",
}));

const { RecentJobs } = await import("../job-list");
const { CleanupPanel } = await import("../cleanup-panel");
const { TaskPick } = await import("../task-pick");
const { PushDialog } = await import("../push-dialog");
type PoolJob = import("../types").PoolJob;

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const serve = (answer: (path: string) => unknown) => {
  globalThis.fetch = mock((input: RequestInfo | URL) =>
    Promise.resolve(
      new Response(JSON.stringify(answer(String(input))), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  ) as unknown as typeof fetch;
};

describe("repeated buttons and tables have names", () => {
  test("the job list has a header row and every row button names its job", async () => {
    const jobs = [
      { id: "job-a", kind: "export", status: "done", resumable: false },
      { id: "job-b", kind: "export", status: "failed", resumable: true },
    ] as PoolJob[];
    const { host } = await render(
      <RecentJobs
        jobs={jobs}
        onChanged={() => {}}
        onPush={() => {}}
        onJob={() => {}}
        onLog={() => {}}
        onNotice={() => {}}
      />,
    );
    const heads = [...host.querySelectorAll("thead th")].map((th) =>
      th.textContent?.trim(),
    );
    expect(heads).toEqual([
      "Select",
      "Job",
      "Type",
      "Status",
      "Output",
      "When",
      "Actions",
    ]);
    for (const row of host.querySelectorAll("tbody tr")) {
      const id = row.querySelector("code")!.textContent!;
      const names = [...row.querySelectorAll("td.pg-pool-actions button")].map(
        (b) => b.getAttribute("aria-label"),
      );
      expect(names.length).toBeGreaterThan(2);
      for (const name of names) expect(name).toContain(`: ${id}`);
    }
    const labels = [...host.querySelectorAll("tbody button")].map((b) =>
      b.getAttribute("aria-label"),
    );
    expect(labels).toContain("Details: job-a");
    expect(labels).toContain("Resume: job-b");
  });

  test("each Delete in the cleanup table names its unfinished output", async () => {
    const partial = (name: string) => ({
      path: `/exports/${name}`,
      name,
      job: null,
      status: null,
      live: false,
      known: false,
      resumable: false,
      bytes: 1,
      age_seconds: 1,
      expires_in_seconds: 1,
    });
    serve(() => ({
      partials: [partial(".mix.partial"), partial(".other.partial")],
      jobs: [],
      temp: [],
      reclaimable_bytes: 0,
      removable_bytes: 0,
      disk: [],
    }));
    const { host } = await render(
      <CleanupPanel refreshKey={0} onChanged={() => {}} onNotice={() => {}} />,
    );
    await waitFor(() => host.querySelector("tbody button"), {
      label: "the table",
    });
    const names = [...host.querySelectorAll("tbody button")].map((b) =>
      b.getAttribute("aria-label"),
    );
    expect(names).toEqual(["Delete: .mix.partial", "Delete: .other.partial"]);
  });

  test("Edit and Done name the task they open", async () => {
    const { host } = await render(
      <TaskPick
        entry={{
          task: "stack cups",
          count: null,
          success_ratio: null,
          strategy: "quality",
        }}
        report={undefined}
        onChange={() => {}}
      />,
    );
    const button = host.querySelector<HTMLButtonElement>(
      "button[aria-expanded]",
    )!;
    expect(button.getAttribute("aria-label")).toBe("Edit: stack cups");
    expect(button.textContent).toBe("Edit");
  });
});

describe("the push dialog's targets", () => {
  test("the Delete button is beside the radio's label, not inside it", async () => {
    serve((path) =>
      path.includes("pool/remotes")
        ? {
            targets: [
              { name: "gpu", display: "wk@gpu:/data", host: "gpu", path: "/d" },
            ],
          }
        : {},
    );
    const { host } = await render(
      <PushDialog
        exportJob={{ id: "e1", kind: "export", status: "done" } as PoolJob}
        onClose={() => {}}
      />,
    );
    const radio = await waitFor(
      () => host.querySelector<HTMLInputElement>("input[type=radio]"),
      { label: "the target" },
    );
    const label = radio.closest("label")!;
    expect(label.querySelector("button")).toBeNull();
    const del = host.querySelector("button[aria-label='Delete: gpu']")!;
    expect(del.closest("label")).toBeNull();
    expect(del.parentElement).toBe(label.parentElement);
  });
});
