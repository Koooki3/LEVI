import { describe, expect, test } from "bun:test";
import { createPreviewApi } from "../preview-api";
import { previewView, PREVIEW_SCHEMA } from "../preview-logic";

// The camera preview's client and its document. The fetch is a fake.

const jpeg = () =>
  new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), {
    headers: { "content-type": "image/jpeg", "x-levi-frame-age-ms": "90" },
  });
const notFound = (code: string) =>
  Response.json({ detail: { code, message: "x" } }, { status: 404 });

function apiOf(answer: (url: string) => Promise<Response>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const api = createPreviewApi({
    fetchFn: (url, init) => {
      calls.push({ url, init });
      return answer(url);
    },
  });
  return { api, calls };
}

describe("PreviewApi", () => {
  test("reads are GETs and the two writes are bodiless-in-effect POSTs of their routes", async () => {
    const { api, calls } = apiOf(() => Promise.resolve(Response.json({})));
    await api.read();
    await api.start();
    await api.stop();
    expect(calls.map((c) => [c.init?.method ?? "GET", c.url])).toEqual([
      ["GET", "/api/levi/marvel0/preview"],
      ["POST", "/api/levi/marvel0/preview/start"],
      ["POST", "/api/levi/marvel0/preview/stop"],
    ]);
  });

  test("a refusal keeps its code and facts", async () => {
    const { api } = apiOf(() =>
      Promise.resolve(
        Response.json(
          {
            detail: {
              code: "arm_in_use",
              message: "m",
              reasons: ["arm_in_use"],
            },
          },
          { status: 409 },
        ),
      ),
    );
    const answer = await api.start();
    expect(answer.ok).toBe(false);
    if (answer.ok) return;
    expect(answer.status).toBe(409);
    expect(answer.code).toBe("arm_in_use");
    expect(answer.extra).toEqual({ reasons: ["arm_in_use"] });
  });

  test("a frame is asked from the preview's route, one camera at a time", async () => {
    const { api, calls } = apiOf(() => Promise.resolve(jpeg()));
    const side = await api.frame("side");
    await api.frame("wrist");
    expect(calls.map((c) => c.url)).toEqual([
      "/api/levi/marvel0/preview/frame?camera=side",
      "/api/levi/marvel0/preview/frame?camera=wrist",
    ]);
    expect(side).toMatchObject({ ok: true, ageMs: 90, episode: null });
  });

  test("the preview's two reasons are taken over; the run's are not", async () => {
    for (const [code, reason] of [
      ["preview_off", "preview_off"],
      ["no_frame", "no_frame"],
      ["no_run", "unavailable"],
      ["no_episode", "unavailable"],
      ["something_else", "unavailable"],
    ] as const) {
      const { api } = apiOf(() => Promise.resolve(notFound(code)));
      expect(await api.frame("side")).toEqual({ ok: false, reason });
    }
  });

  test("an answer that is no picture, or no answer, is unavailable", async () => {
    const text = apiOf(() =>
      Promise.resolve(
        new Response("x", { headers: { "content-type": "text/plain" } }),
      ),
    );
    expect(await text.api.frame("side")).toEqual({
      ok: false,
      reason: "unavailable",
    });
    const gone = apiOf(() => Promise.reject(new Error("down")));
    expect(await gone.api.frame("wrist")).toEqual({
      ok: false,
      reason: "unavailable",
    });
  });
});

const doc = (over: Record<string, unknown> = {}) => ({
  schema: PREVIEW_SCHEMA,
  state: "off",
  reason: null,
  may_start: true,
  stopped_by: null,
  limits: { idle_s: 20, max_s: 600 },
  ...over,
});

describe("previewView", () => {
  test("reads the document the backend writes", () => {
    expect(previewView(doc())).toEqual({
      state: "off",
      reason: null,
      mayStart: true,
      stoppedBy: null,
      limits: { idleS: 20, maxS: 600 },
    });
    expect(
      previewView(
        doc({
          state: "refused",
          may_start: false,
          reason: { code: "run_active", text: "An evaluation run" },
          stopped_by: "idle",
        }),
      ),
    ).toMatchObject({
      state: "refused",
      mayStart: false,
      reason: { code: "run_active", text: "An evaluation run" },
      stoppedBy: "idle",
    });
  });

  test.each([
    ["null", null],
    ["another schema", doc({ schema: "levi.marvel0.preview.v2" })],
    ["an unknown state", doc({ state: "paused" })],
    ["a may_start that is not a boolean", doc({ may_start: "yes" })],
    ["limits missing", doc({ limits: null })],
    [
      "a limit that is not finite",
      doc({ limits: { idle_s: "20", max_s: 600 } }),
    ],
  ])("%s is no document", (_, data) => {
    expect(previewView(data)).toBeNull();
  });

  test("a stopped_by it does not know is dropped, not guessed", () => {
    expect(previewView(doc({ stopped_by: "gremlins" }))?.stoppedBy).toBeNull();
  });
});
