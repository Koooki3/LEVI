import { describe, expect, test } from "bun:test";
import {
  DEADLINES,
  createMarvel0Api,
  errorOf,
  type ApiFailure,
} from "../deploy-api";
import { MARVEL0_REQUEST_MS } from "../marvel0-logic";

type Call = { path: string; init?: RequestInit };

/** A fetch that records its calls and answers from `answer`. */
function fake(answer: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const fetchFn = (async (input: unknown, init?: RequestInit) => {
    const call = { path: String(input), init };
    calls.push(call);
    return answer(call);
  }) as typeof fetch;
  return { calls, fetchFn };
}
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });

describe("errorOf", () => {
  test("reads the shape of LEVI's own refusals", () => {
    const failure = errorOf(409, {
      detail: {
        code: "port_taken",
        message: "Something already listens",
        port: 8000,
        listeners: 1,
        ours: false,
      },
    });
    expect(failure).toEqual({
      ok: false,
      status: 409,
      code: "port_taken",
      message: "Something already listens",
      extra: { port: 8000, listeners: 1, ours: false },
      field: null,
      fields: [],
    });
  });

  test("keeps the field of a form refusal out of the extra facts", () => {
    const failure = errorOf(422, {
      detail: {
        code: "max_steps_out_of_range",
        message: "max_steps must be from 1 to 3000",
        field: "max_steps",
        min: 1,
        max: 3000,
      },
    });
    expect(failure.field).toBe("max_steps");
    expect(failure.extra).toEqual({ min: 1, max: 3000 });
  });

  test("reads FastAPI's list of body errors into fields", () => {
    const failure = errorOf(422, {
      detail: [
        {
          type: "int_type",
          loc: ["body", "max_steps"],
          msg: "Input should be a valid integer",
        },
        {
          type: "extra_forbidden",
          loc: ["body", "levi_mode"],
          msg: "Extra inputs are not permitted",
        },
      ],
    });
    expect(failure.code).toBe("validation");
    expect(failure.fields).toEqual([
      { field: "max_steps", message: "Input should be a valid integer" },
      { field: "levi_mode", message: "Extra inputs are not permitted" },
    ]);
    expect(failure.field).toBe("max_steps");
  });

  test("a plain text detail, an empty body and a non-object body keep the status", () => {
    expect(errorOf(500, { detail: "boom" })).toMatchObject({
      code: "http_500",
      message: "boom",
    });
    expect(errorOf(404, {})).toMatchObject({
      code: "http_404",
      message: "HTTP 404",
    });
    expect(errorOf(502, null)).toMatchObject({ code: "http_502" });
    expect(errorOf(502, "bad gateway")).toMatchObject({ code: "http_502" });
  });

  test("a code that is not a string is not taken for one", () => {
    expect(errorOf(400, { detail: { code: 7, message: 3 } })).toMatchObject({
      code: "http_400",
      message: "HTTP 400",
    });
  });
});

describe("the deadlines", () => {
  test("a read is given up when the status cards give theirs up; a stop outlasts the server's own wait", () => {
    expect(DEADLINES.read).toBe(MARVEL0_REQUEST_MS);
    // The server waits up to the unit's stop timeout (at most 600 s) plus 30 s,
    // but the page's site default is 90 s: the page waits beyond 120 s.
    expect(DEADLINES.stop).toBeGreaterThan(120_000);
    expect(DEADLINES.deploy).toBeGreaterThan(15_000);
  });
});

describe("the client", () => {
  test("reads are uncached GETs of LEVI's own routes, without a body", async () => {
    const { calls, fetchFn } = fake(() => json({ schema: "x" }));
    const api = createMarvel0Api({ fetchFn });
    expect(await api.models()).toMatchObject({ ok: true, status: 200 });
    await api.site();
    await api.deploy();
    await api.log(80);
    expect(calls.map((call) => call.path)).toEqual([
      "/api/levi/marvel0/models",
      "/api/levi/marvel0/site",
      "/api/levi/marvel0/deploy",
      "/api/levi/marvel0/deploy/log?lines=80",
    ]);
    for (const call of calls) {
      expect(call.init?.method ?? "GET").toBe("GET");
      expect(call.init?.body).toBeUndefined();
      expect(call.init?.cache).toBe("no-store");
    }
  });

  test("writes are JSON POSTs and carry no credentials of their own", async () => {
    const { calls, fetchFn } = fake(() => json({ ok: true }, 202));
    const api = createMarvel0Api({ fetchFn });
    await api.startDeploy({ request_id: "abc" });
    await api.stopDeploy({ request_id: "def" });
    await api.preview({ model_id: "m" });
    expect(calls.map((call) => [call.init?.method, call.path])).toEqual([
      ["POST", "/api/levi/marvel0/deploy"],
      ["POST", "/api/levi/marvel0/deploy/stop"],
      ["POST", "/api/levi/marvel0/command/preview"],
    ]);
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      request_id: "abc",
    });
    for (const call of calls) {
      const headers = new Headers(call.init?.headers);
      expect([...headers.keys()]).toEqual(["content-type"]);
      expect(call.init?.credentials).toBeUndefined();
    }
  });

  test("a refusal becomes a failure with the status, code and facts", async () => {
    const { fetchFn } = fake(() =>
      json(
        {
          detail: {
            code: "busy",
            message: "Another deploy or stop is running",
            operation: null,
          },
        },
        423,
      ),
    );
    const result = await createMarvel0Api({ fetchFn }).startDeploy({});
    expect(result).toMatchObject({ ok: false, status: 423, code: "busy" });
  });

  test("a body that is not JSON is a failure with the status line", async () => {
    const { fetchFn } = fake(() => new Response("<html>", { status: 502 }));
    const result = (await createMarvel0Api({ fetchFn }).deploy()) as ApiFailure;
    expect(result).toMatchObject({ ok: false, status: 502, code: "http_502" });
  });

  test("no answer at all is status 0, code network", async () => {
    const { fetchFn } = fake(() => {
      throw new TypeError("fetch failed");
    });
    const result = (await createMarvel0Api({ fetchFn }).deploy()) as ApiFailure;
    expect(result).toMatchObject({
      ok: false,
      status: 0,
      code: "network",
      message: "fetch failed",
    });
  });

  test("a request that never answers is given up at its deadline, even if it ignores its signal", async () => {
    const { fetchFn } = fake(() => new Promise<Response>(() => {}));
    const api = createMarvel0Api({ fetchFn, deadlines: { read: 20 } });
    const result = (await api.deploy()) as ApiFailure;
    expect(result).toMatchObject({ ok: false, status: 0, code: "network" });
    expect(result.message).toContain("no answer");
  });

  test("an aborted read throws, so a caller that left sees no result", async () => {
    const { fetchFn } = fake(
      (call) =>
        new Promise<Response>((_, reject) => {
          call.init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    const controller = new AbortController();
    const pending = createMarvel0Api({ fetchFn }).deploy(controller.signal);
    controller.abort();
    await expect(pending).rejects.toBeDefined();
  });

  test("a success whose body is not JSON still counts as an answer, with no data", async () => {
    const { fetchFn } = fake(() => new Response("", { status: 202 }));
    const result = await createMarvel0Api({ fetchFn }).startDeploy({});
    expect(result).toMatchObject({ ok: true, status: 202, data: null });
  });
});
