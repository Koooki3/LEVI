import {
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { LocaleProvider } from "@/components/levi-locale";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/workbench",
}));

// A plain anchor: the framework's link prefetches by observing visibility,
// which updates state outside the test's act().
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

const OWN = {
  id: "local/own",
  name: "own",
  path: "/w/datasets/own",
  kind: "lerobot",
};
const LINKED = {
  id: "local/live.run1",
  name: "live.run1",
  path: "/live-ws/captures/run1",
  kind: "raw",
  view_status: "ready",
  linked: {
    kind: "live",
    source: "run1",
    workspace: "levi-live-ws",
    readonly: true,
  },
};

function serve(linkedWorkspaces: unknown[], requests: string[] = []) {
  globalThis.fetch = mock((input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    requests.push(`${init?.method ?? "GET"} ${path}`);
    if (path.includes("/convert/formats"))
      return Promise.resolve(
        json({ inputs: [], outputs: [], unsupported: [] }),
      );
    if (path.includes("/catalog"))
      return Promise.resolve(
        json({
          local: [OWN, LINKED],
          workspace: "/w",
          conversion_available: true,
          stages: [],
          linked: linkedWorkspaces,
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
  return requests;
}

const rowOf = (host: HTMLElement, name: string) =>
  [...host.querySelectorAll("tbody tr")].find((row) =>
    row.textContent?.includes(name),
  ) as HTMLElement;

describe("Local datasets with the live evaluation workspace linked", () => {
  test("a linked dataset says what it is, and its changing buttons say why they are off", async () => {
    const requests = serve([
      { kind: "live", workspace: "levi-live-ws", datasets: 1 },
    ]);
    const { host } = await render(<Workbench />);
    const linked = await waitFor(() => {
      const row = rowOf(host, "live.run1");
      return row ?? false;
    });
    expect(linked.querySelector(".ds-badge")?.textContent).toContain(
      "Live evaluation · read-only",
    );
    // The path line is the source, not a local path to copy.
    expect(linked.textContent).toContain(
      "From the live evaluation workspace levi-live-ws",
    );
    expect(linked.textContent).not.toContain("/live-ws/captures/run1");
    // Its viewer still opens.
    expect(linked.querySelector("a")?.getAttribute("href")).toBe(
      "/local/live.run1",
    );
    const buttons = [...linked.querySelectorAll("button")];
    expect(buttons.map((b) => b.textContent)).toEqual([
      "Use as input",
      "Unregister",
    ]);
    const why = linked.querySelector("#wb-linked-why-live\\.run1")!;
    expect(why.textContent).toBe(
      "Read-only: belongs to the live evaluation workspace",
    );
    for (const button of buttons) {
      expect(button.disabled).toBe(true);
      expect(button.getAttribute("aria-describedby")).toBe(why.id);
    }
    // A dataset of the user's own keeps both buttons, enabled, and its path.
    const own = rowOf(host, "own");
    expect(own.textContent).toContain("/w/datasets/own");
    expect(own.querySelector(".ds-badge")?.textContent ?? "").not.toContain(
      "Live evaluation",
    );
    for (const button of own.querySelectorAll("button"))
      expect((button as HTMLButtonElement).disabled).toBe(false);
    // Nothing was sent that changes the catalog.
    expect(requests.filter((r) => r.startsWith("DELETE"))).toEqual([]);
  });

  test("one sentence above the list names the workspace and the count", async () => {
    serve([{ kind: "live", workspace: "levi-live-ws", datasets: 3 }]);
    const { host } = await render(<Workbench />);
    const note = await waitFor(() => host.querySelector(".pg-note"));
    expect(note.textContent).toContain(
      "live evaluation workspace levi-live-ws",
    );
    expect(note.textContent).toContain("(3)");
    expect(note.textContent).toContain("read-only");
    expect(note.textContent).toContain("not copied");
    expect(note.textContent).toContain("does not need to be running");
  });

  test("no sentence when nothing is linked", async () => {
    serve([]);
    const { host } = await render(<Workbench />);
    await waitFor(() => rowOf(host, "own"));
    expect(host.querySelector(".pg-note")).toBeNull();
  });

  test("Chinese: the badge, the source line and the reason", async () => {
    localStorage.setItem("levi-language", "zh");
    serve([{ kind: "live", workspace: "levi-live-ws", datasets: 1 }]);
    const { host } = await render(
      <LocaleProvider>
        <Workbench />
      </LocaleProvider>,
    );
    const linked = await waitFor(() => rowOf(host, "live.run1"));
    await flush(40);
    expect(linked.textContent).toContain("实时评测 · 只读");
    expect(linked.textContent).toContain("来自实时评测工作区 levi-live-ws");
    expect(linked.textContent).toContain("只读：属于实时评测工作区");
    expect(host.querySelector(".pg-note")!.textContent).toContain(
      "实时服务不用开着也能看",
    );
  });
});
