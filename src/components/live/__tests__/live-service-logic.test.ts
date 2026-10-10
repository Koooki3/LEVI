import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { errorOf } from "../live-service-api";
import { SERVICE_TABLE_KEYS } from "../live-service-block";
import {
  RequestIds,
  codeKey,
  serviceKind,
  startBlock,
  startBody,
  startedBy,
  stopBlock,
  stopBody,
  type ServiceStatus,
} from "../live-service-logic";

const base: ServiceStatus = {
  enabled: true,
  holder: { alive: false },
  unit: { installed: true, state: "inactive" },
  start_checks: [],
  stop_checks: [],
  needs_confirmation: { start: [], stop: [] },
  confirm: {
    start: "start-live",
    stop: "stop-live",
    force_phrase: "stop live during evaluation",
  },
};
const make = (extra: Partial<ServiceStatus>): ServiceStatus => ({
  ...base,
  ...extra,
});

describe("the state of the service", () => {
  test("maps the core's facts to one word", () => {
    expect(serviceKind(base)).toBe("stopped");
    expect(serviceKind(make({ holder: { alive: true } }))).toBe("running");
    expect(
      serviceKind(
        make({ holder: { alive: false }, unit: { state: "activating" } }),
      ),
    ).toBe("starting");
    expect(serviceKind(make({ unit: { state: "failed" } }))).toBe("failed");
    expect(
      serviceKind(make({ unit: { state: "inactive", flapping: true } })),
    ).toBe("failed");
    expect(
      serviceKind(
        make({
          operation: { operation_id: "o", action: "start", state: "running" },
        }),
      ),
    ).toBe("starting");
    // A finished start is not "starting".
    expect(
      serviceKind(
        make({
          operation: { operation_id: "o", action: "start", state: "done" },
        }),
      ),
    ).toBe("stopped");
  });

  test("says who started it", () => {
    expect(startedBy(base)).toBeNull();
    for (const by of ["unit", "terminal", "terminal_once"] as const)
      expect(startedBy(make({ holder: { alive: true, started_by: by } }))).toBe(
        by,
      );
    expect(startedBy(make({ holder: { alive: true, started_by: null } }))).toBe(
      "other",
    );
  });
});

describe("what keeps a button off, in words", () => {
  const gpu = make({ needs_confirmation: { start: ["gpu_shared"], stop: [] } });
  test("a start needs the phrase, and the GPU tick when asked for", () => {
    expect(startBlock(base, { gpuShared: false, phrase: "" }, false)).toBe(
      "liveService.block.needPhrase",
    );
    expect(
      startBlock(base, { gpuShared: false, phrase: "start-live" }, false),
    ).toBeNull();
    expect(
      startBlock(gpu, { gpuShared: false, phrase: "start-live" }, false),
    ).toBe("liveService.block.needGpuConfirm");
    expect(
      startBlock(gpu, { gpuShared: true, phrase: "start-live" }, false),
    ).toBeNull();
  });

  test("a refusal, a request in progress, 'disabled' and 'served by live' win", () => {
    const form = { gpuShared: true, phrase: "start-live" };
    const refused = make({
      start_checks: [{ code: "port_foreign", level: "refuse", message: "x" }],
    });
    expect(startBlock(refused, form, false)).toBe("liveService.block.refused");
    expect(startBlock(base, form, true)).toBe("liveService.block.busy");
    expect(startBlock(make({ enabled: false }), form, false)).toBe(
      "liveService.block.disabled",
    );
    expect(
      startBlock(make({ served_by_live_service: true }), form, false),
    ).toBe("liveService.block.servedByLive");
  });

  test("a stop during an evaluation needs the long phrase as well", () => {
    const active = make({
      needs_confirmation: { start: [], stop: ["evaluation_active"] },
    });
    expect(stopBlock(active, "stop-live", "", false)).toBe(
      "liveService.block.needForcePhrase",
    );
    expect(
      stopBlock(active, "stop-live", "stop live during evaluation", false),
    ).toBeNull();
    expect(stopBlock(base, "stop-live", "", false)).toBeNull();
    expect(stopBlock(base, "stop", "", false)).toBe(
      "liveService.block.needPhrase",
    );
  });

  test("request bodies carry the phrases the core asked for", () => {
    const failed = make({
      needs_confirmation: { start: ["unit_failed", "gpu_shared"], stop: [] },
    });
    expect(startBody(failed, { gpuShared: true, phrase: "x" }, "id-1")).toEqual(
      {
        request_id: "id-1",
        confirm: "start-live",
        confirm_gpu_shared: true,
        reset_failed: true,
      },
    );
    const active = make({
      needs_confirmation: { start: [], stop: ["evaluation_active"] },
    });
    expect(stopBody(active, " stop live during evaluation ", "id-2")).toEqual({
      request_id: "id-2",
      confirm: "stop-live",
      force_phrase: "stop live during evaluation",
    });
    expect(stopBody(base, "ignored", "id-3").force_phrase).toBe("");
  });
});

describe("request ids", () => {
  test("the same intent keeps its id until it is released", () => {
    let n = 0;
    const ids = new RequestIds(() => `id-${++n}`);
    expect(ids.next("start")).toBe("id-1");
    expect(ids.next("start")).toBe("id-1");
    expect(ids.next("stop")).toBe("id-2");
    ids.release();
    expect(ids.next("stop")).toBe("id-3");
  });
});

describe("errors of the core", () => {
  test("detail, error and plain bodies all give a code, a message and an operation", () => {
    expect(
      errorOf(423, {
        detail: { code: "busy", message: "m", operation_id: "op-1" },
      }),
    ).toMatchObject({ code: "busy", message: "m", operationId: "op-1" });
    expect(
      errorOf(409, { error: { code: "E_X", message: "y" } }),
    ).toMatchObject({
      code: "E_X",
      message: "y",
    });
    expect(errorOf(502, { detail: "bad gateway" })).toMatchObject({
      code: "http_502",
      message: "bad gateway",
    });
    expect(errorOf(500, null)).toMatchObject({
      code: "http_500",
      message: "HTTP 500",
    });
  });

  test("known codes have a sentence in both languages", () => {
    expect(codeKey("evaluation_active")).toBe(
      "liveService.code.evaluationActive",
    );
    expect(codeKey("something_new")).toBeNull();
  });
});

describe("the catalogues", () => {
  test("every liveService key used through a table is in both languages", () => {
    const missing = SERVICE_TABLE_KEYS.filter(
      (key) => !(key in en) || !(key in zh),
    );
    expect(missing).toEqual([]);
  });

  test("the two catalogues have the same liveService keys", () => {
    const keys = (catalog: object) =>
      Object.keys(catalog).filter((key) => key.startsWith("liveService."));
    expect(keys(en)).toEqual(keys(zh));
    expect(keys(en).length).toBeGreaterThan(50);
  });
});
