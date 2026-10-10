import {
  click,
  fire,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { fixtureJobs, mockFetch, plan } from "./fixtures";

const pushed: string[] = [];
mock.module("next/navigation", () => ({
  useRouter: () => ({ push: (url: string) => pushed.push(url) }),
}));
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { LaunchPanel } = await import("../launch-panel");
setupDom();

let restore = () => {};
afterEach(() => {
  restore();
  pushed.length = 0;
});
const button = (root: ParentNode, text: string) =>
  Array.from(root.querySelectorAll("button")).find(
    (node) => node.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined;

async function planned(over = {}) {
  const m = mockFetch({
    "GET jobs": () => fixtureJobs(),
    "POST plan": () => plan(over),
    "POST runs": () => ({ run_id: "run-new" }),
  });
  restore = m.restore;
  const { host } = await render(<LaunchPanel />);
  const select = await waitFor(() => {
    const el = host.querySelector("select") as HTMLSelectElement | null;
    return el && el.options.length > 1 ? el : null;
  });
  select.value = "job-1";
  await fire(select, new Event("change", { bubbles: true }));
  await flush();
  await click(button(host, "Make a plan")!);
  await waitFor(() => host.textContent?.includes("Plan"));
  return { host, m };
}

describe("the launch panel", () => {
  test("a dry-run plan can be started, after a confirmation, and opens the run window", async () => {
    const { host, m } = await planned();
    expect(host.textContent).toContain("Not confirmed by a user");
    await click(button(host, "Start run")!);
    const dialog = document.querySelector("[role=alertdialog]")!;
    expect(dialog).toBeTruthy();
    // The same press twice sends one launch.
    const confirmButton = button(dialog, "Start run")!;
    await click(confirmButton);
    await click(confirmButton);
    await waitFor(() => pushed.length > 0);
    expect(pushed).toEqual(["/automatic/runs/run-new"]);
    const launches = m.calls.filter(
      (c) => c.method === "POST" && c.path === "runs",
    );
    expect(launches.length).toBe(1);
    expect(launches[0].body).toMatchObject({
      job_id: "job-1",
      confirm: "launch",
      launch_token: "tok",
    });
    expect(typeof (launches[0].body as { request_id: string }).request_id).toBe(
      "string",
    );
  });
  test("a plan that moves the robot cannot be started: the button is off and says why", async () => {
    const { host } = await planned({
      execution_mode: "autonomous",
      motion: true,
    });
    const start = button(host, "Start run")!;
    expect(start.disabled).toBe(true);
    expect(host.textContent).toContain("no real robot adapter");
    await click(start);
    expect(document.querySelector("[role=alertdialog]")).toBeNull();
  });
  test("a refused plan lists the reasons and cannot be started", async () => {
    const { host } = await planned({
      launchable: false,
      refusals: ["isc_missing"],
    });
    expect(button(host, "Start run")!.disabled).toBe(true);
    expect(host.textContent).toContain("isc_missing");
  });
  test("a changed plan (412) is reported and the plan is dropped", async () => {
    const { host, m } = await planned();
    m.restore();
    const again = mockFetch({
      "POST runs": () => ({
        __status: 412,
        body: { detail: { code: "plan_changed", message: "x" } },
      }),
    });
    restore = again.restore;
    await click(button(host, "Start run")!);
    await click(
      button(document.querySelector("[role=alertdialog]")!, "Start run")!,
    );
    await waitFor(() =>
      host.textContent?.includes("The job changed after planning"),
    );
    expect(pushed).toEqual([]);
    expect(button(host, "Start run")).toBeUndefined();
  });
});
