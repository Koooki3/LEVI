import { flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  usePathname: () => "/",
}));
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));

const { ConfirmProvider } = await import("../shell/confirm");
const { default: AgentConnections } = await import("../agent-connections");
const { default: AgentObjectTool } = await import("../agent-object-tool");
const { default: AgentPilot } = await import("../agent-pilot");
const { default: AgentRuntimeConnections } =
  await import("../agent-runtime-connections");
const { default: Workbench } = await import("../../app/workbench/page");

setupDom();

const original = globalThis.fetch;
const originalSource = (globalThis as { EventSource?: unknown }).EventSource;
afterEach(() => {
  globalThis.fetch = original;
  (globalThis as { EventSource?: unknown }).EventSource = originalSource;
});

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));

/** The accessible name a screen reader gives each button. */
const names = (root: ParentNode) =>
  [...root.querySelectorAll("button")].map(
    (b) => b.getAttribute("aria-label") ?? b.textContent?.trim() ?? "",
  );
const namesOf = (root: ParentNode, label: string) =>
  names(root).filter((n) => n.startsWith(label));

function expectDistinct(list: string[], count: number) {
  expect(list).toHaveLength(count);
  expect(new Set(list).size).toBe(count);
}

describe("repeated buttons have distinct accessible names", () => {
  test("two model connections: every action names its connection", async () => {
    globalThis.fetch = mock(() =>
      json({ external: null }),
    ) as unknown as typeof fetch;
    const provider = (name: string) => ({
      kind: "openai-local" as const,
      name,
      model: "m",
      base_url: "http://127.0.0.1:1",
      vision: false,
      tools: false,
      enabled: true,
      credential_ready: true,
      credential_source: "not_required",
      key_env: "X",
      allow_localhost: true,
    });
    const { host } = await render(
      <ConfirmProvider>
        <AgentConnections
          providers={[provider("alpha"), provider("beta")]}
          selected="alpha"
          select={() => undefined}
          refresh={async () => undefined}
          edit={() => undefined}
        />
      </ConfirmProvider>,
    );
    await flush(10);
    expectDistinct(namesOf(host, "Disconnect:"), 2);
    expectDistinct(namesOf(host, "Remove configuration:"), 2);
    expectDistinct(namesOf(host, "Edit configuration:"), 2);
    expect(namesOf(host, "Selected:")).toEqual(["Selected: alpha"]);
    expect(namesOf(host, "Use this model:")).toEqual(["Use this model: beta"]);
  });

  test("two Codex / Claude grants: Disconnect LEVI only names the client", async () => {
    globalThis.fetch = mock((url: string) =>
      String(url).endsWith("/grants")
        ? json(
            ["codex", "claude"].map((client) => ({
              id: client,
              client,
              datasets: ["local/demo"],
              enabled: true,
              expires: Date.now() / 1000 + 3600,
              calls: 0,
              max_tool_calls: 10,
            })),
          )
        : json([]),
    ) as unknown as typeof fetch;
    const { host } = await render(<AgentRuntimeConnections />);
    await waitFor(() => namesOf(host, "Disconnect LEVI only:").length === 2);
    expectDistinct(namesOf(host, "Disconnect LEVI only:"), 2);
  });

  test("object tool: Accept, Reject and Relabel name their track", async () => {
    const row = (id: number) => ({
      object_id: `o${id}`,
      track_id: id,
      concept: "cup",
      status: "proposed",
      score: 0.9,
      mask_rle: { size: [1, 1], counts: [0, 1] },
      bbox_xyxy: [0, 0, 1, 1],
      image_size: [1, 1],
    });
    globalThis.fetch = mock((_url: string, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? "{}"));
      if (body.name === "objects.status") return json({ available: true });
      if (body.name === "objects.get")
        return json({ id: "j1", status: "waiting_for_review" });
      if (body.name === "objects.inspect")
        return json({
          problem_offsets: [],
          frames: 1,
          pending: 2,
          revision: "r",
          rows: [row(1), row(2)],
          evidence: {
            artifact: "a.png",
            episode_index: 0,
            camera_key: "cam",
            frame_index: 0,
            timestamp: 0,
          },
        });
      return json({});
    }) as unknown as typeof fetch;
    const { host } = await render(
      <AgentObjectTool
        runId="r1"
        status="waiting_for_review"
        jobIds={["j1"]}
        refresh={() => undefined}
      />,
    );
    await waitFor(() => namesOf(host, "Accept track:").length === 2);
    expectDistinct(namesOf(host, "Accept track:"), 2);
    expectDistinct(namesOf(host, "Reject track:"), 2);
    expectDistinct(namesOf(host, "Relabel track:"), 2);
    expect(namesOf(host, "Accept track:")[1]).toBe("Accept track: cup #2");
  });

  test("Pilot sessions: Cancel names its session", async () => {
    (globalThis as { EventSource?: unknown }).EventSource = class {
      close() {}
    };
    const session = (id: string) => ({
      id,
      run_id: "r1",
      runtime: "codex",
      state: "running",
      connection: "connected",
      turns: 1,
    });
    globalThis.fetch = mock((url: string) => {
      const path = String(url);
      if (path.endsWith("/pilot/sessions"))
        return json([session("s1"), session("s2")]);
      if (path.endsWith("/tools")) return json({ events: [] });
      if (path.endsWith("/manifest"))
        return json({ status: "empty", artifacts: [] });
      return json([]);
    }) as unknown as typeof fetch;
    const { host } = await render(
      <AgentPilot
        runId="r1"
        approved
        repo="local/x"
        edited={false}
        follow={false}
      />,
    );
    await waitFor(() => namesOf(host, "Cancel:").length === 2);
    expectDistinct(namesOf(host, "Cancel:"), 2);
    expectDistinct(namesOf(host, "Take over / pause:"), 2);
  });

  test("Workbench dataset rows: Use as input and Unregister name the dataset", async () => {
    const own = (name: string) => ({
      id: `local/${name}`,
      name,
      path: `/w/${name}`,
      kind: "lerobot",
    });
    globalThis.fetch = mock((url: string) => {
      const path = String(url);
      if (path.includes("/convert/formats"))
        return json({ inputs: [], outputs: [], unsupported: [] });
      if (path.includes("/catalog"))
        return json({
          local: [own("one"), own("two")],
          workspace: "/w",
          conversion_available: true,
          stages: [],
          linked: [],
        });
      if (path.includes("/sync"))
        return json({
          enabled: true,
          running: true,
          interval_seconds: 5,
          last_scan: null,
          last_error: null,
          pending: [],
          changes: [],
        });
      return json([]);
    }) as unknown as typeof fetch;
    const { host } = await render(<Workbench />);
    await waitFor(() => namesOf(host, "Unregister:").length === 2);
    expectDistinct(namesOf(host, "Unregister:"), 2);
    expectDistinct(namesOf(host, "Use as input:"), 2);
  });
});
