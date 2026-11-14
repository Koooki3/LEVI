import { click, render, setupDom, waitFor } from "../../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/pool",
}));

const { ConfirmProvider } = await import("@/components/shell/confirm");
const { JobBanner } = await import("../job-panel");
const { RecentJobs } = await import("../job-list");
type PoolJob = import("../types").PoolJob;

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

function serve() {
  const posts: string[] = [];
  globalThis.fetch = mock((input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? "GET") === "POST") posts.push(String(input));
    return Promise.resolve(
      new Response(
        JSON.stringify({ id: "export-1", kind: "export", status: "cancelled" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
  }) as unknown as typeof fetch;
  return posts;
}

const stalled = {
  id: "export-1",
  kind: "export",
  status: "stalled",
  partial: "/exports/.mix.partial",
  resumable: true,
  rerunnable: true,
  age_seconds: 400,
} as unknown as PoolJob;

const button = (host: Element, label: string) =>
  [...host.querySelectorAll("button")].find((b) => b.textContent === label);

describe("stopping a job that holds unfinished output", () => {
  test("the banner button says what goes, asks first, and cancels only on yes", async () => {
    const posts = serve();
    const { host } = await render(
      <ConfirmProvider>
        <JobBanner job={stalled} onJob={() => {}} onLog={() => {}} />
      </ConfirmProvider>,
    );
    // The label is visible text, not a hover tooltip.
    expect(button(host, "Cancel")).toBeUndefined();
    await click(button(host, "Stop and delete partial output")!);
    const dialog = await waitFor(
      () => host.querySelector("[role=alertdialog]"),
      {
        label: "the confirmation",
      },
    );
    expect(dialog.textContent).toContain("/exports/.mix.partial");
    expect(dialog.textContent).toContain("export-1");
    expect(posts).toEqual([]);
    await click(button(dialog, "Cancel")!);
    await waitFor(() => !host.querySelector("[role=alertdialog]"), {
      label: "the dialog closing",
    });
    expect(posts).toEqual([]);
    await click(button(host, "Stop and delete partial output")!);
    const again = await waitFor(
      () => host.querySelector("[role=alertdialog]"),
      { label: "the confirmation again" },
    );
    await click(button(again, "Stop and delete partial output")!);
    await waitFor(() => posts.length === 1, { label: "the cancel request" });
    expect(posts[0]).toContain("/pool/jobs/export-1/cancel");
  });

  test("a job with nothing to lose keeps the plain Cancel, with no question", async () => {
    const posts = serve();
    const scan = { id: "scan-1", kind: "scan", status: "stalled" } as PoolJob;
    const { host } = await render(
      <ConfirmProvider>
        <JobBanner job={scan} onJob={() => {}} onLog={() => {}} />
      </ConfirmProvider>,
    );
    await click(button(host, "Cancel")!);
    await waitFor(() => posts.length === 1, { label: "the cancel request" });
    expect(host.querySelector("[role=alertdialog]")).toBeNull();
  });

  test("the job list asks the same question before cancelling", async () => {
    const posts = serve();
    const { host } = await render(
      <ConfirmProvider>
        <RecentJobs
          jobs={[stalled]}
          onChanged={() => {}}
          onPush={() => {}}
          onJob={() => {}}
          onLog={() => {}}
          onNotice={() => {}}
        />
      </ConfirmProvider>,
    );
    await click(button(host, "Stop and delete partial output")!);
    await waitFor(() => host.querySelector("[role=alertdialog]"), {
      label: "the confirmation",
    });
    expect(posts).toEqual([]);
  });
});
