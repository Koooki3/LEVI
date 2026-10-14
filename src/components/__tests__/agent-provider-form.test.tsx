import { fire, flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { LocaleProvider } from "../levi-locale";
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
        model_digest: "sha256:abc",
        base_url: "http://127.0.0.1:8100",
        model: "qwen",
        key_env: "LEVI_LOCAL_MODEL_KEY",
        vision: true,
        tools: true,
        allow_localhost: true,
        context_tokens: 16384,
        max_images: 4,
        image_max_side: 896,
        think: true,
        fold_system: true,
        prompt_style: "lean",
        requests_in_flight: 2,
      } as Provider),
    );
    expect(form()).toMatchObject({
      name: "vllm-local",
      providerKind: "openai-local",
      modelDigest: "sha256:abc",
      url: "http://127.0.0.1:8100",
      model: "qwen",
      keyEnv: "LEVI_LOCAL_MODEL_KEY",
      vision: true,
      supportsTools: true,
      allowLocal: true,
      contextTokens: 16384,
      maxImages: 4,
      imageMaxSide: 896,
      think: true,
      foldSystem: true,
      promptStyle: "lean",
      inFlight: 2,
    });
  });

  test("local-only settings are sent as set for vLLM and dropped for a cloud API", async () => {
    const calls = core([]);
    const { host, form } = await show();
    const local = {
      name: "v",
      kind: "openai-local",
      base_url: "http://127.0.0.1:8100",
      model: "m",
      key_env: "LEVI_LOCAL_MODEL_KEY",
      vision: false,
      tools: false,
      allow_localhost: true,
      max_images: 4,
      image_max_side: 896,
      think: true,
      fold_system: true,
      prompt_style: "lean",
      requests_in_flight: 2,
    } as Provider;
    const submit = () =>
      fire(
        host.querySelector("form"),
        new Event("submit", { bubbles: true, cancelable: true }),
      );
    await act(async () => form().fill(local));
    await submit();
    await flush(20);
    expect(calls.filter((c) => c.method === "POST")[0].body).toMatchObject({
      structured_output: true,
      max_images: 4,
      image_max_side: 896,
      think: true,
      fold_system: true,
      prompt_style: "lean",
      requests_in_flight: 2,
    });
    await act(async () => form().fill({ ...local, kind: "openai-compatible" }));
    await submit();
    await flush(20);
    expect(calls.filter((c) => c.method === "POST")[1].body).toMatchObject({
      structured_output: false,
      max_images: null,
      image_max_side: null,
      think: false,
      fold_system: false,
      prompt_style: "full",
      requests_in_flight: 1,
    });
  });

  test("Chinese: the Ollama and vLLM hints are translated", async () => {
    localStorage.setItem("levi-language", "zh");
    try {
      let form: ProviderFormState | null = null;
      function Harness() {
        const f = useProviderForm();
        form = f;
        return (
          <ProviderForm
            form={f}
            act={async () => {}}
            busy={false}
            providers={[]}
            onSaved={() => {}}
          />
        );
      }
      const { host } = await render(
        <LocaleProvider>
          <Harness />
        </LocaleProvider>,
      );
      await flush(20);
      await act(async () => form!.setProviderKind("ollama"));
      expect(host.textContent).not.toContain("Save this connection");
      await act(async () => form!.setProviderKind("openai-local"));
      expect(host.textContent).not.toContain("Use the server root");
    } finally {
      localStorage.removeItem("levi-language");
    }
  });
});
