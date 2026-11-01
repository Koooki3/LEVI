import { describe, expect, test } from "bun:test";
import { FRAME_DEADLINE_MS, createRunApi } from "../run-api";

// `RunApi.frame`: the live view's one binary read. The fetch is a fake.

const jpeg = (headers: Record<string, string> = {}) =>
  new Response(new Uint8Array([0xff, 0xd8, 0xff, 0xd9]), {
    headers: { "content-type": "image/jpeg", ...headers },
  });
const notFound = (code: string) =>
  Response.json({ detail: { code, message: "x" } }, { status: 404 });

function apiOf(answer: (url: string, init?: RequestInit) => Promise<Response>) {
  const urls: string[] = [];
  const api = createRunApi({
    fetchFn: (url, init) => {
      urls.push(url);
      return answer(url, init);
    },
  });
  return { api, urls };
}

describe("RunApi.frame", () => {
  test("asks the route of one camera and keeps the bytes and the two facts", async () => {
    const { api, urls } = apiOf(() =>
      Promise.resolve(
        jpeg({
          "x-levi-frame-age-ms": "240",
          "x-levi-frame-episode": "demo_0007",
        }),
      ),
    );
    const side = await api.frame("side");
    const wrist = await api.frame("wrist");
    expect(urls).toEqual([
      "/api/levi/marvel0/run/frame?camera=side",
      "/api/levi/marvel0/run/frame?camera=wrist",
    ]);
    if (!side.ok) throw new Error("no picture");
    expect(side.ageMs).toBe(240);
    expect(side.episode).toBe("demo_0007");
    expect(side.blob.type).toBe("image/jpeg");
    expect(new Uint8Array(await side.blob.arrayBuffer())).toEqual(
      new Uint8Array([0xff, 0xd8, 0xff, 0xd9]),
    );
    expect(wrist.ok).toBe(true);
  });

  test("a header that is not what the backend writes is not taken over", async () => {
    const { api } = apiOf(() =>
      Promise.resolve(
        jpeg({
          "x-levi-frame-age-ms": "-3",
          "x-levi-frame-episode": "../../etc/passwd",
        }),
      ),
    );
    const result = await api.frame("side");
    expect(result).toMatchObject({ ok: true, ageMs: null, episode: null });
  });

  test("a 404 carries its reason; any other answer is 'unavailable'", async () => {
    for (const code of ["no_run", "no_episode", "no_frame"]) {
      const { api } = apiOf(() => Promise.resolve(notFound(code)));
      expect(await api.frame("side")).toEqual({ ok: false, reason: code });
    }
    for (const response of [
      notFound("something_else"),
      Response.json({ detail: "x" }, { status: 500 }),
      new Response("<html>bad gateway</html>", { status: 502 }),
      new Response("not a picture", {
        headers: { "content-type": "text/plain" },
      }),
      new Response(new Uint8Array(), {
        headers: { "content-type": "image/jpeg" },
      }),
    ]) {
      const { api } = apiOf(() => Promise.resolve(response));
      expect(await api.frame("wrist")).toEqual({
        ok: false,
        reason: "unavailable",
      });
    }
    const { api } = apiOf(() => Promise.reject(new Error("offline")));
    expect(await api.frame("wrist")).toEqual({
      ok: false,
      reason: "unavailable",
    });
  });

  test("the caller's abort rejects; a request nobody answers is given up", async () => {
    const never = () => new Promise<Response>(() => {});
    const { api } = apiOf(never);
    const controller = new AbortController();
    const pending = api.frame("side", controller.signal);
    controller.abort();
    await expect(pending).rejects.toThrow();
    await expect(api.frame("side", controller.signal)).rejects.toThrow();

    const realSetTimeout = globalThis.setTimeout;
    let deadline = 0;
    globalThis.setTimeout = ((fn: () => void, ms?: number) => {
      deadline = ms ?? 0;
      return realSetTimeout(fn, 0);
    }) as unknown as typeof setTimeout;
    try {
      expect(await api.frame("wrist")).toEqual({
        ok: false,
        reason: "unavailable",
      });
    } finally {
      globalThis.setTimeout = realSetTimeout;
    }
    expect(deadline).toBe(FRAME_DEADLINE_MS);
  });
});
