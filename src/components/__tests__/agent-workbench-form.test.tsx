import { act } from "react";
import { click, flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  usePathname: () => "/local/demo/1",
}));

const { ConfirmProvider } = await import("../shell/confirm");
const { ToastProvider } = await import("../ds");
const { SHELL_EVENTS } = await import("../shell/shell-events");
const { default: AgentWorkbench, parseEpisodes } =
  await import("../agent-workbench");

setupDom();

describe("parseEpisodes", () => {
  test("an emptied field stays empty instead of becoming episode 0", () => {
    expect(parseEpisodes("")).toEqual([]);
    expect(parseEpisodes(" , ,")).toEqual([]);
    expect(parseEpisodes("0")).toEqual([0]);
    expect(parseEpisodes("3, 1,x,-2,1.5")).toEqual([3, 1]);
  });
});

const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});

function stubFetch(calls: Array<{ url: string; body: string }> = []) {
  globalThis.fetch = mock((url: string, init?: RequestInit) => {
    const href = String(url);
    calls.push({ url: href, body: String(init?.body ?? "") });
    const json = (value: unknown) =>
      Promise.resolve(new Response(JSON.stringify(value)));
    if (href.endsWith("/meta/info.json"))
      return json({ total_episodes: 3, features: {} });
    if (href.includes("/meta/tasks.jsonl"))
      return Promise.resolve(new Response("", { status: 404 }));
    return json([]);
  }) as unknown as typeof fetch;
}

async function openDrawer() {
  const view = await render(
    <ToastProvider>
      <ConfirmProvider>
        <AgentWorkbench />
      </ConfirmProvider>
    </ToastProvider>,
  );
  await act(async () => {
    window.dispatchEvent(new Event(SHELL_EVENTS.agentToggle));
  });
  await flush(10);
  return view;
}

const button = (label: string) =>
  Array.from(document.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === label,
  ) as HTMLButtonElement | undefined;

describe("the plan form needs an episode", () => {
  test("clearing the episode chips blocks Inspect & create plan with a reason", async () => {
    stubFetch();
    await openDrawer();
    await waitFor(() => button("Clear"), { label: "the episode chips" });
    // The page's own episode (1) is chosen; with a provider the form is ready.
    const submit = () => button("Inspect & create plan")!;
    expect(document.body.textContent).not.toContain(
      "Choose at least one episode",
    );
    await click(button("Clear")!);
    await flush(10);
    expect(submit().disabled).toBe(true);
    expect(document.body.textContent).toContain("Choose an agent model first.");
  });

  test("with a provider, the reason names the episode and no plan request is sent", async () => {
    const calls: Array<{ url: string; body: string }> = [];
    stubFetch(calls);
    await openDrawer();
    await waitFor(() => button("Clear"), { label: "the episode chips" });
    const selects = Array.from(document.querySelectorAll("select"));
    const model = selects.find((s) =>
      Array.from(s.options).some((o) => o.value === "external"),
    )!;
    await act(async () => {
      model.value = "external";
      model.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click(button("Clear")!);
    await flush(10);
    const submit = button("Inspect & create plan")!;
    expect(submit.disabled).toBe(true);
    expect(document.body.textContent).toContain("Choose at least one episode");
    expect(submit.getAttribute("aria-describedby")).toBeTruthy();
    await click(submit);
    await flush(10);
    expect(calls.filter((c) => c.url.endsWith("/tools"))).toEqual([]);
  });
});
