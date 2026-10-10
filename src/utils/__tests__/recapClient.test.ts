import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  mock,
  test,
} from "bun:test";
import {
  cancelRecapJob,
  fetchRecapCompare,
  fetchRecapEpisode,
  fetchRecapJob,
  fetchRecapRevisions,
  fetchRecapStatus,
  fetchRecapSummary,
  RecapRecomputedError,
  fetchRecapSettings,
  runRecap,
  saveRecapSettings,
} from "@/utils/annotationsClient";

const globals = globalThis as unknown as {
  window?: unknown;
  fetch: typeof fetch;
};
const originalWindow = globals.window;
const originalFetch = globals.fetch;

function setWindow(value: unknown) {
  // Another suite may leave `window` non-writable; redefine instead.
  Object.defineProperty(globalThis, "window", { value, configurable: true });
}

type Call = { url: string; init?: RequestInit };
let calls: Call[] = [];

function respond(status: number, body: unknown) {
  globals.fetch = mock((input: string | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return Promise.resolve(
      new Response(typeof body === "string" ? body : JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  }) as unknown as typeof fetch;
}

const ident = { repoId: "local/plates" };
const job = {
  id: "20260926T2300",
  repo_id: "local/plates",
  checkpoint: "rlinf-fr3-step3000",
  status: "running",
  progress: { stage: "values", done: 3, total: 10 },
  error: null,
  revision_id: null,
  created_at: 1790000000,
};

beforeAll(() => setWindow({ location: { origin: "http://levi.test" } }));

beforeEach(() => {
  calls = [];
});

afterAll(() => {
  globals.fetch = originalFetch;
  setWindow(originalWindow);
});

describe("RECAP client", () => {
  test("saved results are a read-only uncached request with cancellation", async () => {
    const payload = { current: "20261009-120000", revisions: [] };
    respond(200, payload);
    const controller = new AbortController();
    expect(await fetchRecapRevisions(ident, controller.signal)).toEqual(
      payload,
    );
    expect(new URL(calls[0].url).pathname).toBe(
      "/api/annotation/recap/results",
    );
    expect(calls[0].init?.method).toBeUndefined();
    expect(calls[0].init?.cache).toBe("no-store");
    expect(calls[0].init?.signal).toBe(controller.signal);
  });

  test("an older backend without /results is asked under the old name", async () => {
    const payload = { current: null, revisions: [{ revision_id: "x" }] };
    let n = 0;
    globals.fetch = mock((input: string | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      n += 1;
      return Promise.resolve(
        n === 1
          ? new Response('{"detail":"Not Found"}', { status: 404 })
          : new Response(JSON.stringify(payload), { status: 200 }),
      );
    }) as unknown as typeof fetch;
    expect((await fetchRecapRevisions(ident)).revisions).toEqual(
      payload.revisions,
    );
    expect(calls.map((call) => new URL(call.url).pathname)).toEqual([
      "/api/annotation/recap/results",
      "/api/annotation/recap/revisions",
    ]);
  });

  test("a list sent only under the new name 'results' still fills revisions", async () => {
    respond(200, { current: null, results: [{ revision_id: "m" }] });
    expect((await fetchRecapRevisions(ident)).revisions).toEqual([
      { revision_id: "m" } as never,
    ]);
  });

  test("pinned reads send their version and turn 409 into 'recomputed'", async () => {
    respond(200, null);
    await fetchRecapEpisode(3, ident, undefined, "model-a", "20261010-1000");
    await fetchRecapSummary(ident, undefined, "model-a", "20261010-1000");
    for (const call of calls)
      expect(new URL(call.url).searchParams.get("version")).toBe(
        "20261010-1000",
      );
    calls = [];
    respond(200, { frames: { shared: 0 } });
    await fetchRecapCompare(ident, "model-a", "model-b", undefined, {
      a: "20261010-1000",
      b: "20261009-1000",
    });
    const url = new URL(calls[0].url);
    expect(url.searchParams.get("version_a")).toBe("20261010-1000");
    expect(url.searchParams.get("version_b")).toBe("20261009-1000");
    respond(409, {
      detail: "The model-a result was recomputed; reload it",
      code: "recomputed",
    });
    for (const read of [
      () => fetchRecapEpisode(3, ident, undefined, "model-a", "20261010-1000"),
      () => fetchRecapSummary(ident, undefined, "model-a", "20261010-1000"),
      () =>
        fetchRecapCompare(ident, "model-a", "model-b", undefined, {
          a: "20261010-1000",
        }),
    ]) {
      const error = await read().catch((e: unknown) => e);
      expect(error).toBeInstanceOf(RecapRecomputedError);
      expect((error as Error).message).toContain("recomputed");
    }
    // Unpinned, a 409 is an ordinary refusal (e.g. different sources).
    const plain = await fetchRecapCompare(ident, "a", "b").catch(
      (e: unknown) => e,
    );
    expect(plain).not.toBeInstanceOf(RecapRecomputedError);
  });

  test("a pinned read refused for another reason is an ordinary error with its own text", async () => {
    for (const detail of [
      "The dataset changed between these revisions; recompute both on the same source",
      "A published revision is missing episode 4",
      "Shared frame timestamps differ between these revisions",
    ]) {
      respond(409, { detail });
      const error = await fetchRecapCompare(ident, "m-a", "m-b", undefined, {
        a: "20261010-1000",
        b: "20261009-1000",
      }).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(Error);
      expect(error).not.toBeInstanceOf(RecapRecomputedError);
      expect((error as Error).message).toBe(detail);
      const episode = await fetchRecapEpisode(
        1,
        ident,
        undefined,
        "m-a",
        "20261010-1000",
      ).catch((e: unknown) => e);
      expect(episode).not.toBeInstanceOf(RecapRecomputedError);
    }
  });

  test("the dataset label rule is read and stored through /recap/settings", async () => {
    const choice = {
      setting: "sft",
      dataset_type: "sft",
      source: "user",
      reason: "the dataset setting",
    };
    respond(200, choice);
    expect(await fetchRecapSettings(ident)).toEqual(choice);
    const read = new URL(calls[0].url);
    expect(read.pathname).toBe("/api/annotation/recap/settings");
    expect(read.searchParams.get("repo_id")).toBe("local/plates");
    expect(calls[0].init?.method).toBeUndefined();
    await saveRecapSettings(ident, "auto");
    expect(new URL(calls[1].url).pathname).toBe(
      "/api/annotation/recap/settings",
    );
    expect(calls[1].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[1].init?.body))).toEqual({
      repo_id: "local/plates",
      dataset_type: "auto",
    });
    respond(400, {
      detail: "dataset_type is auto, rollout, sft or value_only",
    });
    await expect(saveRecapSettings(ident, "sft")).rejects.toThrow(
      "dataset_type is auto",
    );
  });

  test("a computation without a label rule sends none: the backend decides", async () => {
    respond(200, job);
    await runRecap(ident, { checkpoint: "value-r2" });
    const body = JSON.parse(String(calls[0].init?.body));
    expect("dataset_type" in body).toBe(false);
  });

  test("comparison carries both explicit revisions and surfaces refused pairings", async () => {
    respond(200, { dataset: "plates", frames: { shared: 5 } });
    const controller = new AbortController();
    await fetchRecapCompare(
      ident,
      "20261008-120000",
      "20261009-120000",
      controller.signal,
    );
    const url = new URL(calls[0].url);
    expect(url.pathname).toBe("/api/annotation/recap/compare");
    expect(url.searchParams.get("repo_id")).toBe("local/plates");
    expect(url.searchParams.get("a")).toBe("20261008-120000");
    expect(url.searchParams.get("b")).toBe("20261009-120000");
    expect(calls[0].init?.signal).toBe(controller.signal);
    expect(calls[0].init?.method).toBeUndefined();
    respond(409, { detail: "Dataset sources differ between revisions" });
    await expect(fetchRecapCompare(ident, "a", "b")).rejects.toThrow(
      "Dataset sources differ between revisions",
    );
  });

  test("historical summary and episode keep source revision and result revision separate", async () => {
    respond(200, null);
    const source = { ...ident, revision: "source-version" };
    const controller = new AbortController();
    await fetchRecapSummary(source, controller.signal, "20261008-120000");
    await fetchRecapEpisode(5, source, controller.signal, "20261008-120000");
    for (const call of calls) {
      const url = new URL(call.url);
      expect(url.searchParams.get("revision_id")).toBe("20261008-120000");
      expect(url.searchParams.get("revision")).toBe("source-version");
      expect(url.searchParams.get("optional")).toBe("true");
      expect(call.init?.signal).toBe(controller.signal);
    }
  });

  test("an SFT computation sends its label rule without adding a manual threshold", async () => {
    respond(200, job);
    await runRecap(ident, { checkpoint: "value-r2", dataset_type: "sft" });
    const body = JSON.parse(String(calls[0].init?.body));
    expect(body.dataset_type).toBe("sft");
    expect(body.checkpoint).toBe("value-r2");
    expect(body.threshold).toBeNull();
  });
  test("status goes through the annotation proxy with repo_id", async () => {
    respond(200, {
      checkpoints: [],
      worker: { ready: true, reason: null },
      current: null,
      job: null,
    });
    const status = await fetchRecapStatus(ident);
    expect(status.current).toBeNull();
    const url = new URL(calls[0].url);
    expect(url.pathname).toBe("/api/annotation/recap/status");
    expect(url.searchParams.get("repo_id")).toBe("local/plates");
  });

  test("run posts the contract body with defaults", async () => {
    respond(200, job);
    const result = await runRecap(ident, { checkpoint: "rlinf-fr3-step3000" });
    expect(result.id).toBe(job.id);
    expect(new URL(calls[0].url).pathname).toBe("/api/annotation/recap/run");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      repo_id: "local/plates",
      checkpoint: "rlinf-fr3-step3000",
      episodes: null,
      lookahead: 10,
      positive_quantile: 0.3,
      threshold: null,
    });
  });

  test("run surfaces the backend's readable detail", async () => {
    respond(400, { detail: "Checkpoint is not ready: missing weights" });
    await expect(runRecap(ident, { checkpoint: "x" })).rejects.toThrow(
      "Checkpoint is not ready: missing weights",
    );
    respond(409, { detail: "A RECAP job is already running" });
    await expect(runRecap(ident, { checkpoint: "x" })).rejects.toThrow(
      "already running",
    );
  });

  test("a validation error list reads as its messages", async () => {
    respond(422, {
      detail: [{ msg: "field required" }, { msg: "value is not a float" }],
    });
    await expect(runRecap(ident, { checkpoint: "x" })).rejects.toThrow(
      "field required; value is not a float",
    );
  });

  test("a long non-JSON body falls back to a status message", async () => {
    respond(502, "<html>" + "x".repeat(400) + "</html>");
    await expect(fetchRecapStatus(ident)).rejects.toThrow("RECAP status: 502");
  });

  test("job polling and cancel hit the job routes", async () => {
    respond(200, job);
    await fetchRecapJob(job.id, ident);
    expect(new URL(calls[0].url).pathname).toBe(
      `/api/annotation/recap/jobs/${job.id}`,
    );
    respond(200, { ...job, status: "cancelled" });
    const cancelled = await cancelRecapJob(job.id, ident);
    expect(cancelled.status).toBe("cancelled");
    expect(new URL(calls[1].url).pathname).toBe(
      `/api/annotation/recap/jobs/${job.id}/cancel`,
    );
    expect(calls[1].init?.method).toBe("POST");
  });

  test("summary and episode report a 404 as null", async () => {
    respond(404, { detail: "No advantage labels for this dataset yet" });
    expect(await fetchRecapSummary(ident)).toBeNull();
    expect(await fetchRecapEpisode(5, ident)).toBeNull();
    expect(new URL(calls[1].url).pathname).toBe(
      "/api/annotation/recap/episodes/5",
    );
  });

  test("summary and episode ask with optional=true and read null", async () => {
    respond(200, null);
    expect(await fetchRecapSummary(ident)).toBeNull();
    expect(await fetchRecapEpisode(5, ident)).toBeNull();
    for (const call of calls) {
      const url = new URL(call.url);
      expect(url.searchParams.get("optional")).toBe("true");
      expect(url.searchParams.get("repo_id")).toBe("local/plates");
    }
  });

  test("other failures still throw", async () => {
    respond(500, "boom");
    await expect(fetchRecapEpisode(5, ident)).rejects.toThrow("boom");
    await expect(fetchRecapSummary(ident)).rejects.toThrow();
  });

  test("episode payload is returned as is", async () => {
    const episode = {
      episode_index: 5,
      revision_id: "r1",
      fps: 10,
      threshold: -0.01,
      frame_index: [0, 1],
      timestamp: [0, 0.1],
      value: [-0.6, -0.5],
      advantage: [0.004, -0.2],
      positive: [true, false],
    };
    respond(200, episode);
    expect(await fetchRecapEpisode(5, ident)).toEqual(episode);
  });
});
