import { describe, expect, test } from "bun:test";
import { createSpeechApi } from "../speech-api";
import { createMarvel0Api } from "../deploy-api";
import { encodeWav16 } from "../voice-capture";

type Call = { path: string; init?: RequestInit };

/** A fetch that records its calls and answers from `answer`. */
function fake(
  answer: (call: Call) => Response | Promise<Response> = () => json({}),
) {
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

const BASE = "/api/levi/marvel0";

describe("the voice client", () => {
  test("reads the state with a plain GET: no body, no method, no cache", async () => {
    const { calls, fetchFn } = fake();
    await createSpeechApi({ fetchFn }).status();
    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe(`${BASE}/speech`);
    expect(calls[0].init?.method).toBeUndefined();
    expect(calls[0].init?.body).toBeUndefined();
    expect(calls[0].init?.cache).toBe("no-store");
  });

  test("sends the clip as the raw body of one POST, with Content-Type audio/wav and nothing else in the headers", async () => {
    const { calls, fetchFn } = fake();
    const clip = encodeWav16(Int16Array.of(1, 2, 3));
    await createSpeechApi({ fetchFn }).transcribe(clip, {
      requestId: "req-12345678",
      modelId: "pi05_fr3_all_step49999",
    });
    const call = calls[0];
    expect(call.path).toBe(
      `${BASE}/speech/transcribe?request_id=req-12345678&model_id=pi05_fr3_all_step49999`,
    );
    expect(call.init?.method).toBe("POST");
    expect(call.init?.body).toBe(clip); // the Blob itself, not JSON text
    expect(call.init?.headers).toEqual({ "Content-Type": "audio/wav" });
    expect(call.init?.cache).toBe("no-store");
  });

  test("leaves the model out when there is none, and encodes what it sends", async () => {
    const { calls, fetchFn } = fake();
    await createSpeechApi({ fetchFn }).transcribe(
      encodeWav16(new Int16Array(4)),
      {
        requestId: "a b&c=d.1234",
        modelId: "",
      },
    );
    expect(calls[0].path).toBe(
      `${BASE}/speech/transcribe?request_id=a+b%26c%3Dd.1234`,
    );
  });

  test("relabels a clip of another type as audio/wav", async () => {
    const { calls, fetchFn } = fake();
    const odd = new Blob([new Uint8Array(10)], {
      type: "application/octet-stream",
    });
    await createSpeechApi({ fetchFn }).transcribe(odd, {
      requestId: "req-12345678",
      modelId: "m",
    });
    expect((calls[0].init?.body as Blob).type).toBe("audio/wav");
    expect(calls[0].init?.headers).toEqual({ "Content-Type": "audio/wav" });
  });

  test("matches text with a JSON body, and releases with an empty one", async () => {
    const { calls, fetchFn } = fake();
    const api = createSpeechApi({ fetchFn });
    await api.match({ transcript: "pick the eggplant", modelId: "m1" });
    await api.release();
    expect(calls[0].path).toBe(`${BASE}/speech/match`);
    expect(calls[0].init?.method).toBe("POST");
    expect(calls[0].init?.headers).toEqual({
      "Content-Type": "application/json",
    });
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      transcript: "pick the eggplant",
      model_id: "m1",
    });
    expect(calls[1].path).toBe(`${BASE}/speech/release`);
    expect(calls[1].init?.method).toBe("POST");
  });

  test("sends no credential: the same-origin bridge adds the person's token", async () => {
    const { calls, fetchFn } = fake();
    const api = createSpeechApi({ fetchFn });
    await api.status();
    await api.transcribe(encodeWav16(new Int16Array(4)), {
      requestId: "req-12345678",
      modelId: "m",
    });
    await api.match({ transcript: "x", modelId: "m" });
    await api.release();
    for (const call of calls) {
      const headers = Object.keys(
        (call.init?.headers as Record<string, string>) ?? {},
      );
      expect(
        headers.every((name) => name.toLowerCase() === "content-type"),
      ).toBe(true);
      expect(call.path.startsWith(`${BASE}/speech`)).toBe(true);
    }
  });

  test("reads LEVI's refusals of the voice routes (code and facts)", async () => {
    const { fetchFn } = fake(() =>
      json(
        {
          detail: {
            code: "speech_busy",
            message: "One at a time",
            retry_after: 3,
          },
        },
        429,
      ),
    );
    const result = await createSpeechApi({ fetchFn }).transcribe(
      encodeWav16(new Int16Array(4)),
      { requestId: "req-12345678", modelId: "m" },
    );
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.status).toBe(429);
      expect(result.code).toBe("speech_busy");
      expect(result.extra).toEqual({ retry_after: 3 });
    }
  });

  test("an aborted call throws, and the request is aborted with it", async () => {
    let seen: AbortSignal | undefined;
    const { fetchFn } = fake((call) => {
      seen = call.init?.signal ?? undefined;
      return new Promise<Response>(() => {});
    });
    const controller = new AbortController();
    const api = createSpeechApi({ fetchFn });
    const pending = api.transcribe(
      encodeWav16(new Int16Array(4)),
      { requestId: "req-12345678", modelId: "m" },
      controller.signal,
    );
    controller.abort();
    await expect(pending).rejects.toThrow();
    expect(seen?.aborted).toBe(true);
  });

  test("a call that is never answered is given up at its own deadline", async () => {
    const { fetchFn } = fake(() => new Promise<Response>(() => {}));
    const api = createSpeechApi({
      fetchFn,
      deadlines: { transcribe: 20, status: 20 },
    });
    const result = await api.transcribe(encodeWav16(new Int16Array(4)), {
      requestId: "req-12345678",
      modelId: "m",
    });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.code).toBe("network");
      expect(result.message).toContain("no answer within");
    }
    const status = await api.status();
    expect(status.ok).toBe(false);
  });

  test("the policy-server client still sends JSON as before", async () => {
    const { calls, fetchFn } = fake();
    await createMarvel0Api({ fetchFn }).preview({ model_id: "m" });
    expect(calls[0].init?.headers).toEqual({
      "Content-Type": "application/json",
    });
    expect(calls[0].init?.body).toBe(JSON.stringify({ model_id: "m" }));
  });
});

test("status is scoped to the form model and transcription includes the cold-start budget", async () => {
  const { calls, fetchFn } = fake();
  await createSpeechApi({ fetchFn }).status(undefined, "model & one");
  expect(calls[0].path).toBe(`${BASE}/speech?model_id=model+%26+one`);
  const limits: number[] = [];
  const api = createSpeechApi({
    call: async (_path, deadline) => {
      limits.push(deadline);
      return { ok: true, status: 200, data: {} };
    },
  });
  await api.transcribe(new Blob(), { requestId: "req-12345678", modelId: "m" });
  expect(limits).toEqual([75_000]);
});
