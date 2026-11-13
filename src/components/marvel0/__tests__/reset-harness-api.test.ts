import { describe, expect, test } from "bun:test";
import { createResetHarnessApi } from "../reset-harness-api";

describe("separate offline reset API", () => {
  test("uses only the four offline routes and closed request bodies", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const api = createResetHarnessApi({
      fetchFn: async (url, init) => {
        calls.push({ url, init });
        return new Response(JSON.stringify({ accepted: true }));
      },
    });
    await api.catalog();
    await api.run("offline-1");
    await api.start({
      request_id: "request-1",
      scenario_id: "normal_random",
      seed: 15,
      execution_mode: "live",
    } as Parameters<typeof api.start>[0]);
    await api.stop("offline-1", {
      request_id: "request-2",
      physical_execution: true,
    } as Parameters<typeof api.stop>[1]);
    expect(calls.map((c) => c.url)).toEqual([
      "/api/levi/marvel0/reset-harness",
      "/api/levi/marvel0/reset-harness/runs/offline-1",
      "/api/levi/marvel0/reset-harness/runs",
      "/api/levi/marvel0/reset-harness/runs/offline-1/stop",
    ]);
    expect(calls.map((c) => c.init?.method ?? "GET")).toEqual([
      "GET",
      "GET",
      "POST",
      "POST",
    ]);
    expect(JSON.parse(calls[2].init!.body as string)).toEqual({
      request_id: "request-1",
      scenario_id: "normal_random",
      seed: 15,
    });
    expect(JSON.parse(calls[3].init!.body as string)).toEqual({
      request_id: "request-2",
    });
    expect(calls.every((c) => c.init?.cache === "no-store")).toBe(true);
    expect(Object.keys(api).sort()).toEqual([
      "catalog",
      "run",
      "start",
      "stop",
    ]);
  });
  test("preserves refusal details and never retries writes automatically", async () => {
    let calls = 0;
    const api = createResetHarnessApi({
      fetchFn: async () => {
        calls++;
        return new Response(
          JSON.stringify({
            detail: { code: "offline_busy", message: "Offline worker is busy" },
          }),
          { status: 409 },
        );
      },
    });
    expect(
      await api.start({
        request_id: "req-1234",
        scenario_id: "normal_random",
        seed: 0,
      }),
    ).toMatchObject({ ok: false, status: 409, code: "offline_busy" });
    expect(calls).toBe(1);
  });
  test("a request deadline returns a failure without falling back to live", async () => {
    const calls: string[] = [];
    const api = createResetHarnessApi({
      deadline: 5,
      fetchFn: (url) => {
        calls.push(url);
        return new Promise(() => {});
      },
    });
    expect(await api.run("offline-1")).toMatchObject({
      ok: false,
      code: "network",
    });
    expect(calls).toHaveLength(1);
  });
});
