import { fire, flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { act } from "react";
import {
  ProviderForm,
  useProviderForm,
  type ProviderFormState,
} from "../agent-provider-form";
import type { Provider } from "../agent-workbench-api";

setupDom();

const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});

type Call = { method: string; path: string; body?: Record<string, unknown> };
function core(list: Provider[]) {
  const calls: Call[] = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push({
      method: init?.method ?? "GET",
      path: String(url).replace("/api/levi/agent/v1", ""),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    return new Response(JSON.stringify(init?.method ? {} : list));
  }) as unknown as typeof fetch;
  return calls;
}

// The model settings moved out of agent-workbench.tsx unchanged; these pin
// what they did there: the connection-type defaults, the saved profile, and
// filling the form from a saved one.
async function show(saved: [Provider[], string][] = []) {
  let form: ProviderFormState | null = null;
  function Harness() {
    const f = useProviderForm();
    form = f;
    return (
      <ProviderForm
        form={f}
        act={async (fn) => {
          await fn();
        }}
        busy={false}
        providers={[]}
        onSaved={(list, name) => saved.push([list, name])}
      />
    );
  }
  const view = await render(<Harness />);
  return { ...view, form: () => form! };
}
const values = (host: HTMLElement) =>
  [
    ...host.querySelectorAll<HTMLInputElement>("input:not([type=checkbox])"),
  ].map((i) => i.value);

describe("the model settings form", () => {
  test("choosing Ollama fills its local defaults", async () => {
    const { host, form } = await show();
    const kind = host.querySelector<HTMLSelectElement>("select")!;
    kind.value = "ollama";
    await fire(kind, new Event("change", { bubbles: true }));
    expect(form().providerKind).toBe("ollama");
    expect(values(host)).toEqual(
      expect.arrayContaining([
        "ollama-local",
        "http://127.0.0.1:11434",
        "qwen3.5:4b",
      ]),
    );
    expect(form().allowLocal).toBe(true);
    expect(form().supportsTools).toBe(false);
  });

  test("a local vLLM gets its own key variable, never the cloud one", async () => {
    const { host, form } = await show();
    const kind = host.querySelector<HTMLSelectElement>("select")!;
    kind.value = "openai-local";
    await fire(kind, new Event("change", { bubbles: true }));
    expect(form().keyEnv).toBe("LEVI_LOCAL_MODEL_KEY");
    expect(form().contextTokens).toBe(32768);
    kind.value = "openai-compatible";
    await fire(kind, new Event("change", { bubbles: true }));
    expect(form().keyEnv).toBe("LEVI_MODEL_API_KEY");
  });

  test("saving posts the whole profile and hands back the new list", async () => {
    const list = [{ name: "model" } as Provider];
    const calls = core(list);
    const saved: [Provider[], string][] = [];
    const { host } = await show(saved);
    await fire(
      host.querySelector("form"),
      new Event("submit", { bubbles: true, cancelable: true }),
    );
    await flush(20);
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe("/providers");
    expect(post.body).toMatchObject({
      name: "model",
      kind: "openai-compatible",
      key_env: "LEVI_MODEL_API_KEY",
      structured_output: false,
      // Local-only settings are not sent for a cloud API.
      max_images: null,
      image_max_side: null,
      think: false,
      fold_system: false,
      prompt_style: "full",
      requests_in_flight: 1,
    });
    expect(saved).toEqual([[list, "model"]]);
  });

  test("Edit on a saved profile fills every field from it", async () => {
    const { form } = await show();
    await act(async () =>
      form().fill({
        name: "vllm-local",
        kind: "openai-local",
        base_url: "http://127.0.0.1:8100",
        model: "qwen",
        key_env: "LEVI_LOCAL_MODEL_KEY",
        vision: true,
        tools: false,
        allow_localhost: true,
        context_tokens: 16384,
        requests_in_flight: 2,
        think: true,
      } as Provider),
    );
    expect(form()).toMatchObject({
      name: "vllm-local",
      providerKind: "openai-local",
      url: "http://127.0.0.1:8100",
      model: "qwen",
      vision: true,
      allowLocal: true,
      contextTokens: 16384,
      inFlight: 2,
      think: true,
      foldSystem: false,
      promptStyle: "full",
    });
  });
});
