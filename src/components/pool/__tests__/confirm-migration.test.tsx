import {
  click,
  press,
  render,
  setupDom,
  waitFor,
} from "../../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));

const { RecentJobs } = await import("../job-list");
const { CleanupPanel } = await import("../cleanup-panel");
type PoolJob = import("../types").PoolJob;

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

const dialog = (host: Element) => host.querySelector("[role=alertdialog]");
const byText = (host: Element, text: string) =>
  [...host.querySelectorAll("button")].find((b) => b.textContent === text) as
    | HTMLButtonElement
    | undefined;

describe("the pool's confirmations are the design system's", () => {
  test("Delete record and files: an alertdialog, focus on Cancel, the plan inside, Escape cancels", async () => {
    let release: () => void = () => {};
    const gate = new Promise<void>((r) => (release = r));
    globalThis.fetch = mock(async (input: RequestInfo | URL) => {
      if (String(input).includes("delete-preview")) {
        await gate;
        return json({
          id: "job-a",
          kind: "export",
          status: "done",
          running: false,
          record_files: 1,
          record_bytes: 10,
          outputs: [],
          refused: null,
          needs_force: false,
          freed_bytes: 2048,
        });
      }
      return json({});
    }) as unknown as typeof fetch;
    const { host } = await render(
      <RecentJobs
        jobs={[{ id: "job-a", kind: "export", status: "done" } as PoolJob]}
        onChanged={() => {}}
        onPush={() => {}}
        onJob={() => {}}
        onLog={() => {}}
        onNotice={() => {}}
      />,
    );
    await click(byText(host, "Delete record and files")!);
    const box = await waitFor(() => dialog(host), { label: "the dialog" });
    expect(host.querySelector(".pg-pool-confirm-actions")).toBeNull();
    expect(document.activeElement?.textContent).toBe("Cancel");
    // Until the plan has loaded there is nothing to confirm.
    const confirmButton = () =>
      [...dialog(host)!.querySelectorAll("button")].find(
        (b) => b.textContent === "Delete record and files",
      )!;
    expect(confirmButton().hasAttribute("disabled")).toBe(true);
    expect(box.textContent).toContain("Checking…");
    release();
    await waitFor(() => box.textContent?.includes("2.0 KB"), {
      label: "the plan",
    });
    expect(confirmButton().hasAttribute("disabled")).toBe(false);
    expect(box.textContent).toContain("2.0 KB");
    await press(document.activeElement, "Escape");
    await waitFor(() => !dialog(host), { label: "the dialog closed" });
  });

  test("Cleanup: Delete asks with the folder shown, Cancel first", async () => {
    globalThis.fetch = mock(async () =>
      json({
        partials: [
          {
            path: "/exports/.mix.partial",
            name: ".mix.partial",
            job: null,
            status: null,
            live: false,
            known: false,
            resumable: true,
            bytes: 1,
            age_seconds: 1,
            expires_in_seconds: 1,
          },
        ],
        jobs: [],
        temp: [],
        reclaimable_bytes: 0,
        removable_bytes: 0,
        disk: [],
      }),
    ) as unknown as typeof fetch;
    const { host } = await render(
      <CleanupPanel refreshKey={0} onChanged={() => {}} onNotice={() => {}} />,
    );
    const del = await waitFor(() => host.querySelector("tbody button"), {
      label: "the Delete button",
    });
    await click(del);
    const box = await waitFor(() => dialog(host), { label: "the dialog" });
    expect(box.textContent).toContain("/exports/.mix.partial");
    expect(document.activeElement?.textContent).toBe("Cancel");
    expect(host.querySelector(".pg-pool-confirm-actions")).toBeNull();
  });
});

describe("the bulk clear button names what it clears", () => {
  test("failed, interrupted and cancelled jobs are counted, others are not", async () => {
    const jobs = ["failed", "interrupted", "cancelled", "done", "running"].map(
      (status, i) => ({ id: `j${i}`, kind: "export", status }) as PoolJob,
    );
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
    const bulk = [...host.querySelectorAll(".pg-pool-bulk button")].find((b) =>
      b.textContent?.startsWith("Clear failed"),
    )!;
    expect(bulk.textContent).toBe(
      "Clear failed, interrupted and cancelled jobs (3)",
    );
  });
});
