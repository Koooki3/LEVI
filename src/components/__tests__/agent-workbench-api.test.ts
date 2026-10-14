import { afterEach, describe, expect, test } from "bun:test";
import { api, base, tool } from "../agent-workbench-api";

const original = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = original;
});

type Call = { url: string; init?: RequestInit };
function answer(body: unknown, status = 200) {
  const calls: Call[] = [];
  globalThis.fetch = (async (url: string, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return new Response(JSON.stringify(body), { status });
  }) as unknown as typeof fetch;
  return calls;
}

// These pin the behaviour the request helpers had inside agent-workbench.tsx,
// so a later merge with the other agent request helpers cannot change it unseen.
describe("the Agent Workbench request helpers", () => {
  test("a read is a GET that skips the cache", async () => {
    const calls = answer({ ok: 1 });
    expect(await api("/runs")).toEqual({ ok: 1 });
    expect(calls[0].url).toBe(`${base}/runs`);
    expect(calls[0].init).toEqual({ cache: "no-store" });
  });

  test("a write is a JSON POST", async () => {
    const calls = answer({});
    await api("/runs", { a: 1 });
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.body).toBe(JSON.stringify({ a: 1 }));
    expect(
      (calls[0].init?.headers as Record<string, string>)["Content-Type"],
    ).toBe("application/json");
  });

  test("a refusal throws its text detail as is, and an object detail as JSON", async () => {
    answer({ detail: "Refused: approve the plan first" }, 409);
    await expect(api("/runs/x/start", {})).rejects.toThrow(
      /^Refused: approve the plan first$/,
    );
    answer({ detail: { code: "busy", run: "r1" } }, 423);
    await expect(api("/runs/x/start", {})).rejects.toThrow(
      /^\{"code":"busy","run":"r1"\}$/,
    );
  });

  test("a tool call posts its name, arguments and idempotency key", async () => {
    const calls = answer({ result: 1 });
    await tool("runs.prepare", { repo_id: "r" }, "key-1");
    expect(calls[0].url).toBe(`${base}/tools`);
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      name: "runs.prepare",
      arguments: { repo_id: "r" },
      idempotency_key: "key-1",
    });
  });
});
