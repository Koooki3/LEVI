import {
  click,
  fire,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { mockFetch, plan } from "./fixtures";

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

const jobs = () => ({
  roots: ["jobs"],
  jobs: [
    {
      id: "job-a",
      name: "plates-eval",
      valid: true,
      reset_mode: "human_assisted",
    },
    {
      id: "job-b",
      name: "cups-eval",
      valid: true,
      reset_mode: "human_assisted",
    },
  ],
});

async function choose(host: HTMLElement, id: string) {
  const select = host.querySelector("select") as HTMLSelectElement;
  select.value = id;
  await fire(select, new Event("change", { bubbles: true }));
  await flush();
}

describe("the launch panel ties a plan to its job", () => {
  test("the Select is off while planning, and a late answer for another job is not shown", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const m = mockFetch({
      "GET jobs": () => jobs(),
      "POST plan": () => gate,
    });
    restore = m.restore;
    const { host } = await render(<LaunchPanel />);
    await waitFor(() => host.querySelectorAll("option").length > 1);
    await choose(host, "job-a");
    await click(button(host, "Make a plan")!);
    const select = host.querySelector("select") as HTMLSelectElement;
    expect(select.disabled).toBe(true);
    // Even if the value moves anyway (the control is off, but the guard must
    // not rely on it), the answer for job A is not shown under job B.
    await choose(host, "job-b");
    release(plan({ run_id: "run-for-a" }));
    await flush(20);
    expect(host.textContent).not.toContain("run-for-a");
    expect(host.querySelector("#ar-plan-title")).toBeNull();
    expect(select.disabled).toBe(false);
  });

  test("the plan shows for its own job and is hidden when another job is picked", async () => {
    const m = mockFetch({
      "GET jobs": () => jobs(),
      "POST plan": () => plan(),
    });
    restore = m.restore;
    const { host } = await render(<LaunchPanel />);
    await waitFor(() => host.querySelectorAll("option").length > 1);
    await choose(host, "job-a");
    await click(button(host, "Make a plan")!);
    await waitFor(() => host.querySelector("#ar-plan-title"));
    await choose(host, "job-b");
    expect(host.querySelector("#ar-plan-title")).toBeNull();
  });

  test("the launch confirmation names the job, the episodes and the mode", async () => {
    const m = mockFetch({
      "GET jobs": () => jobs(),
      "POST plan": () => plan({ episodes: 20 }),
      "POST runs": () => ({ run_id: "run-new" }),
    });
    restore = m.restore;
    const { host } = await render(<LaunchPanel />);
    await waitFor(() => host.querySelectorAll("option").length > 1);
    await choose(host, "job-a");
    await click(button(host, "Make a plan")!);
    await waitFor(() => button(host, "Start run"));
    await click(button(host, "Start run")!);
    const dialog = document.querySelector("[role=alertdialog]")!;
    expect(dialog.textContent).toContain("Job plates-eval: 20 episodes");
    expect(dialog.textContent).toContain("Dry run (no robot)");
    // The gate itself is unchanged: still needs the confirm button.
    expect(m.calls.filter((c) => c.path === "runs").length).toBe(0);
  });
});
