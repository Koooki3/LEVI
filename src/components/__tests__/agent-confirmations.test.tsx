import { click, flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  usePathname: () => "/",
}));

const { ConfirmProvider } = await import("../shell/confirm");
const { default: AgentReviewQueue } = await import("../agent-review-queue");
const { default: AgentConnections } = await import("../agent-connections");
const { default: AgentRuntimeConnections } =
  await import("../agent-runtime-connections");

setupDom();

const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});

const button = (root: ParentNode, label: string) =>
  [...root.querySelectorAll("button")].find((b) =>
    b.textContent?.trim().startsWith(label),
  ) as HTMLButtonElement | undefined;
const dialog = () => document.querySelector('[role="alertdialog"]');

describe("Accept visible / Reject visible", () => {
  const proposal = (content: string) => ({
    episode_index: 0,
    kind: "segment",
    content,
    start: 0,
    end: 2,
    evidence_ids: [],
  });
  const view = (onDecision: (i: number[], d: string) => void) => (
    <ConfirmProvider>
      <AgentReviewQueue
        proposals={[proposal("a"), proposal("b"), proposal("c")]}
        decisions={{}}
        disabled={false}
        onChange={() => undefined}
        onDecision={onDecision}
        onEvidence={() => undefined}
      />
    </ConfirmProvider>
  );

  test("the label carries the count and declining records nothing", async () => {
    const onDecision = mock<(i: number[], d: string) => void>();
    const { host } = await render(view(onDecision));
    expect(button(host, "Accept visible")!.textContent).toBe(
      "Accept visible (3)",
    );
    expect(button(host, "Reject visible")!.textContent).toBe(
      "Reject visible (3)",
    );
    await click(button(host, "Accept visible")!);
    expect(dialog()!.textContent).toContain("Accept 3 visible suggestions?");
    await click(button(dialog()!, "Cancel")!);
    await flush(10);
    expect(onDecision).not.toHaveBeenCalled();
  });

  test("confirming records the visible indices", async () => {
    const onDecision = mock<(i: number[], d: string) => void>();
    const { host } = await render(view(onDecision));
    await click(button(host, "Reject visible")!);
    expect(dialog()!.textContent).toContain("Reject 3 visible suggestions?");
    await click(button(dialog()!, "Reject")!);
    await flush(10);
    expect(onDecision).toHaveBeenCalledWith([0, 1, 2], "rejected");
  });
});

describe("Disconnect asks first", () => {
  const PROVIDER = {
    kind: "openai-local" as const,
    name: "vllm-local",
    model: "qwen",
    base_url: "http://127.0.0.1:18199",
    vision: false,
    tools: false,
    enabled: true,
    credential_ready: true,
    credential_source: "not_required",
    key_env: "X",
    allow_localhost: true,
  };

  test("a model connection: No sends nothing, Yes sends one disconnect", async () => {
    const calls: string[] = [];
    globalThis.fetch = mock((url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      return Promise.resolve(new Response(JSON.stringify({ external: null })));
    }) as unknown as typeof fetch;
    const { host } = await render(
      <ConfirmProvider>
        <AgentConnections
          providers={[PROVIDER]}
          selected=""
          select={() => undefined}
          refresh={async () => undefined}
          edit={() => undefined}
        />
      </ConfirmProvider>,
    );
    await flush(10);
    await click(button(host, "Disconnect")!);
    expect(dialog()!.textContent).toContain("Disconnect vllm-local?");
    await click(button(dialog()!, "Cancel")!);
    await flush(10);
    expect(calls.filter((c) => c.includes("/disconnect"))).toEqual([]);
    await click(button(host, "Disconnect")!);
    await click(button(dialog()!, "Disconnect")!);
    await flush(10);
    expect(calls.filter((c) => c.includes("/disconnect"))).toEqual([
      "POST /api/levi/agent/v1/providers/vllm-local/disconnect",
    ]);
  });

  test("the external Agent asks; reconnecting does not", async () => {
    const calls: string[] = [];
    let enabled = true;
    globalThis.fetch = mock((url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      return Promise.resolve(
        new Response(
          JSON.stringify({
            external: {
              configured: true,
              enabled,
              datasets: [],
              grants: [],
            },
          }),
        ),
      );
    }) as unknown as typeof fetch;
    const { host } = await render(
      <ConfirmProvider>
        <AgentConnections
          providers={[]}
          selected=""
          select={() => undefined}
          refresh={async () => undefined}
          edit={() => undefined}
        />
      </ConfirmProvider>,
    );
    await waitFor(() => button(host, "Disconnect external Agent"));
    const posts = () => calls.filter((c) => c.startsWith("POST"));
    await click(button(host, "Disconnect external Agent")!);
    await click(button(dialog()!, "Cancel")!);
    await flush(10);
    expect(posts()).toEqual([]);
    await click(button(host, "Disconnect external Agent")!);
    await click(button(dialog()!, "Disconnect")!);
    await flush(10);
    expect(posts().length).toBe(1);
    enabled = false;
    await waitFor(() => button(host, "Reconnect external Agent"));
    await click(button(host, "Reconnect external Agent")!);
    await flush(10);
    expect(dialog()).toBeNull();
    expect(posts().length).toBe(2);
  });

  test("a Codex / Claude grant: revoking asks first", async () => {
    const calls: string[] = [];
    globalThis.fetch = mock((url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      const path = String(url);
      return Promise.resolve(
        new Response(
          JSON.stringify(
            path.endsWith("/grants")
              ? [
                  {
                    id: "g1",
                    client: "codex",
                    datasets: ["local/demo"],
                    enabled: true,
                    expires: Date.now() / 1000 + 3600,
                    calls: 0,
                    max_tool_calls: 10,
                  },
                ]
              : [],
          ),
        ),
      );
    }) as unknown as typeof fetch;
    const { host } = await render(
      <ConfirmProvider>
        <AgentRuntimeConnections />
      </ConfirmProvider>,
    );
    await waitFor(() => button(host, "Disconnect LEVI only"));
    await click(button(host, "Disconnect LEVI only")!);
    expect(dialog()!.textContent).toContain("Disconnect codex from LEVI?");
    await click(button(dialog()!, "Cancel")!);
    await flush(10);
    expect(calls.filter((c) => c.includes("/revoke"))).toEqual([]);
    await click(button(host, "Disconnect LEVI only")!);
    await click(button(dialog()!, "Disconnect")!);
    await flush(10);
    expect(calls.filter((c) => c.includes("/revoke"))).toEqual([
      "POST /api/levi/agent/v1/grants/g1/revoke",
    ]);
  });
});
