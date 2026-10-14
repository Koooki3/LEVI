import { flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/",
}));

const { HomeDashboard } = await import("../home-dashboard");

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

function serve(local: unknown[]) {
  globalThis.fetch = mock((input: RequestInfo | URL) => {
    const path = String(input);
    if (path.includes("/catalog")) return Promise.resolve(json({ local }));
    if (path.includes("/pool/jobs")) return Promise.resolve(json({ jobs: [] }));
    if (path.includes("/activity/tasks"))
      return Promise.resolve(json({ tasks: [] }));
    return Promise.resolve(json([]));
  }) as unknown as typeof fetch;
}

describe("the home page's first steps", () => {
  test("a new workspace gets four steps, three links and one button for the agent workbench", async () => {
    serve([]);
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    const card = host.querySelector(".levi-home-first")!;
    expect(card).not.toBeNull();
    expect(card.textContent).toContain("Getting started");
    const steps = [...card.querySelectorAll("li")];
    expect(steps).toHaveLength(4);
    expect(
      steps
        .map((li) => li.querySelector("a")?.getAttribute("href") ?? "button")
        .join(" "),
    ).toBe("/explore /workbench button /guide");
    expect(steps[2].textContent).toContain("Connect a model or agent");
  });

  test("opening a public dataset first does not take the steps away", async () => {
    localStorage.setItem(
      "levi-recent",
      JSON.stringify([
        { repo: "lerobot/svla_so101_pickplace", episode: 0, at: 1 },
      ]),
    );
    serve([]);
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    expect(host.querySelector(".levi-home-first")).not.toBeNull();
  });

  test("once there is a dataset the card is gone", async () => {
    serve([
      {
        id: "local/own",
        name: "own",
        path: "/w/own",
        kind: "lerobot",
        format: { episodes: 3 },
      },
    ]);
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    expect(host.querySelector(".levi-home-first")).toBeNull();
    expect(host.textContent).not.toContain("Getting started");
  });

  test("nothing is shown before the catalog answers, and not when it cannot be read", async () => {
    globalThis.fetch = mock(() =>
      Promise.resolve(json({ detail: "boom" }, 500)),
    ) as unknown as typeof fetch;
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    expect(host.querySelector(".levi-home-first")).toBeNull();
  });
});
