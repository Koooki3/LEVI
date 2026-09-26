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
  fetchRecapEpisode,
  fetchRecapJob,
  fetchRecapStatus,
  fetchRecapSummary,
  runRecap,
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
