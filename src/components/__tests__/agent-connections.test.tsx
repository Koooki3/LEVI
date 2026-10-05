import { click, flush, render, setupDom } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => undefined }),
  usePathname: () => "/",
}));

const { ConfirmProvider } = await import("../shell/confirm");
const { default: AgentConnections } = await import("../agent-connections");

setupDom();

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

describe("removing a model configuration asks first", () => {
  const original = globalThis.fetch;
  let calls: Array<{ url: string; method: string }> = [];
  afterEach(() => {
    globalThis.fetch = original;
  });
  function stub() {
    calls = [];
    globalThis.fetch = mock((url: string, init?: RequestInit) => {
      calls.push({ url: String(url), method: init?.method ?? "GET" });
      return Promise.resolve(new Response(JSON.stringify({ external: null })));
    }) as unknown as typeof fetch;
  }
  const deletes = () => calls.filter((c) => c.method === "DELETE");
  const named = (root: ParentNode, label: string) =>
    Array.from(root.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === label,
    ) as HTMLButtonElement | undefined;
  const view = (inside: boolean) => {
    const content = (
      <AgentConnections
        providers={[PROVIDER]}
        selected=""
        select={() => undefined}
        refresh={async () => undefined}
        edit={() => undefined}
      />
    );
    return inside ? <ConfirmProvider>{content}</ConfirmProvider> : content;
  };

  test("No sends nothing; the name is in the description, not after the question", async () => {
    stub();
    const { host } = await render(view(true));
    await flush(10);
    await click(named(host, "Remove configuration")!);
    const dialog = document.querySelector('[role="alertdialog"]')!;
    expect(dialog.querySelector(".ds-dialog__title")!.textContent).toBe(
      "Remove this configuration?",
    );
    expect(dialog.textContent).toContain("vllm-local");
    await click(named(dialog, "Cancel")!);
    await flush(10);
    expect(deletes()).toEqual([]);
  });

  test("Yes sends one DELETE to the provider", async () => {
    stub();
    const { host } = await render(view(true));
    await flush(10);
    await click(named(host, "Remove configuration")!);
    const dialog = document.querySelector('[role="alertdialog"]')!;
    await click(named(dialog, "Remove configuration")!);
    await flush(10);
    expect(deletes().map((c) => c.url)).toEqual([
      "/api/levi/agent/v1/providers/vllm-local",
    ]);
  });

  test("outside a ConfirmProvider the answer is no", async () => {
    stub();
    const { host } = await render(view(false));
    await flush(10);
    await click(named(host, "Remove configuration")!);
    await flush(10);
    expect(document.querySelector('[role="alertdialog"]')).toBeNull();
    expect(deletes()).toEqual([]);
  });
});
