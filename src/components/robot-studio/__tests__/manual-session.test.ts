import { describe, expect, test } from "bun:test";
import {
  FR3_INITIAL_JOINTS,
  forwardPose,
} from "@/lib/robot-studio/fr3-kinematics";
import {
  createManualApi,
  validManualAnchor,
  ManualApiError,
  type ManualApi,
  type ManualLimits,
  type ManualView,
} from "../manual-api";
import {
  ManualSessionController,
  validManualLimits,
  syntheticStartAllowed,
  manualStartAllowed,
} from "../manual-session";

const attest = { estop: true, scene_reset: true, area_clear: true };
const fixtureLimits = (): ManualLimits => ({
  box_min_m: [-2, -2, -2],
  box_max_m: [2, 2, 2],
  max_delta_m: 0.2,
  max_step_m: 0.01,
  max_velocity_m_s: 0.03,
  period_s: 0.02,
  max_reading_age_s: 0.3,
  max_anchor_age_s: 2,
  deadman_timeout_s: 0.2,
  target_timeout_s: 0.2,
  max_orientation_drift_rad: 0.02,
  quaternion_norm_tolerance: 0.001,
  max_settle_age_s: 0.3,
});
const view = (active = false): ManualView => ({
  schema: "levi.marvel0.manual.v1",
  environment: "synthetic",
  state: active ? "owned_idle" : "ended",
  capabilities: {
    real_motion: false,
    synthetic_only: true,
    can_start: !active,
  },
  reason_code: "",
  admission: active ? "open" : "closed",
  session_id: active ? "synthetic-session" : null,
  start_request_id: null,
  generation: active ? 1 : null,
  anchor_id: active ? "synthetic-anchor" : null,
  anchor: active
    ? {
        // Deliberately differs from nominal FK: delta must use the measured joints.
        position: [0.9, 0.9, 0.9],
        quaternion: [0, 0, 0, 1],
        joints_rad: [...FR3_INITIAL_JOINTS],
        sampled_at_mono: 1,
      }
    : null,
  counters: {},
  stop_evidence: null,
});
const deferred = <T>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
const settle = async () => {
  for (let i = 0; i < 12; i++) await Promise.resolve();
};
function fixture() {
  let time = 0,
    next = 0;
  const timers = new Map<number, { callback: () => void; due: number }>();
  const calls: { kind: string; body?: unknown }[] = [];
  const api: ManualApi = {
    status: async () => {
      calls.push({ kind: "status" });
      return view();
    },
    challenge: async (body) => {
      calls.push({ kind: "challenge", body });
      return { challenge: "synthetic-nonce", confirm: "SYNTHETIC" };
    },
    start: async (body) => {
      calls.push({ kind: "start", body });
      return { ...view(true), start_request_id: body.request_id };
    },
    deadman: async (body) => {
      calls.push({ kind: "deadman", body });
      return view(true);
    },
    target: async (body) => {
      calls.push({ kind: "target", body });
      return view(true);
    },
    stop: async (body) => {
      calls.push({ kind: "stop", body });
      return { ...view(true), state: "held", admission: "closed" };
    },
    end: async (body) => {
      calls.push({ kind: "end", body });
      return { ...view(true), state: "ended", admission: "closed" };
    },
  };
  const controller = new ManualSessionController(api, {
    now: () => time,
    schedule: (callback, delay) => {
      const id = ++next;
      timers.set(id, { callback, due: time + delay });
      return id;
    },
    cancel: (handle) => {
      timers.delete(handle as number);
    },
    requestId: () => "synthetic-request-" + ++next,
  });
  const advance = (ms: number) => {
    time += ms;
    for (const [id, timer] of [...timers]) {
      if (timer.due <= time) {
        timers.delete(id);
        timer.callback();
      }
    }
  };
  const arm = async () => {
    await controller.refresh();
    await controller.review(fixtureLimits());
    await controller.start("SYNTHETIC", attest);
  };
  return { api, controller, calls, advance, arm };
}

describe("person-owned synthetic manual drag lifecycle", () => {
  test("reading a production stop_unproven gate never writes", async () => {
    const f = fixture();
    f.api.status = async () => ({
      ...view(),
      environment: "production",
      state: "unavailable",
      reason_code: "stop_unproven",
      capabilities: {
        can_start: false,
        real_motion: false,
        synthetic_only: true,
      },
    });
    await f.controller.refresh();
    await f.controller.review(fixtureLimits());
    expect(f.controller.snapshot().reason).toBe("stop_unproven");
    expect(f.controller.beginDrag()).toBe(false);
    expect(f.calls).toHaveLength(0);
  });

  test("missing/invalid limits remain invalid, without fallback defaults", () => {
    expect(validManualLimits(null)).toBe(false);
    for (const key of ["period_s", "max_step_m", "target_timeout_s"] as const) {
      expect(validManualLimits({ ...fixtureLimits(), [key]: 0 })).toBe(false);
      expect(validManualLimits({ ...fixtureLimits(), [key]: NaN })).toBe(false);
    }
    expect(
      validManualLimits({
        ...fixtureLimits(),
        box_min_m: [1, 0, 0],
        box_max_m: [0, 1, 1],
      }),
    ).toBe(false);
  });

  test("challenge and explicit attest prepare a session; no stream before a drag", async () => {
    const f = fixture();
    await f.controller.refresh();
    await f.controller.review(fixtureLimits());
    await f.controller.start("wrong", attest);
    expect(f.calls.some((call) => call.kind === "start")).toBe(false);
    await f.controller.start("SYNTHETIC", { ...attest, area_clear: false });
    expect(f.calls.some((call) => call.kind === "start")).toBe(false);
    await f.controller.start("SYNTHETIC", attest);
    expect(f.controller.snapshot().phase).toBe("armed");
    expect(
      f.calls.some((call) => call.kind === "deadman" || call.kind === "target"),
    ).toBe(false);
    f.controller.dispose();
    await settle();
  });

  test("pending pointer updates collapse to one latest delta from measured-joint FK", async () => {
    const f = fixture();
    await f.arm();
    const alive = deferred<ManualView>();
    f.api.deadman = (body) => {
      f.calls.push({ kind: "deadman", body });
      return alive.promise;
    };
    expect(f.controller.beginDrag()).toBe(true);
    const origin = forwardPose(FR3_INITIAL_JOINTS).position;
    f.controller.target([origin[0] + 0.001, origin[1], origin[2]]);
    f.controller.target([origin[0] + 0.002, origin[1], origin[2]]);
    f.controller.target([origin[0] + 0.003, origin[1], origin[2]]);
    f.advance(20);
    expect(f.calls.filter((call) => call.kind === "deadman")).toHaveLength(1);
    alive.resolve(view(true));
    await settle();
    const targets = f.calls.filter((call) => call.kind === "target");
    expect(targets).toHaveLength(1);
    expect(
      (targets[0].body as { delta_position_m: number[] }).delta_position_m[0],
    ).toBeCloseTo(0.003, 10);
    f.controller.revoke("release");
    await settle();
    expect(f.controller.beginDrag()).toBe(false);
    expect(f.controller.snapshot().phase).toBe("closed");
  });

  test("release sends stop without waiting for a blocked target; late replies cannot reopen", async () => {
    const f = fixture();
    await f.arm();
    const moving = deferred<ManualView>();
    f.api.target = (body) => {
      f.calls.push({ kind: "target", body });
      return moving.promise;
    };
    f.controller.beginDrag();
    await settle();
    expect(f.calls.filter((call) => call.kind === "target")).toHaveLength(1);
    f.controller.revoke("release");
    expect(f.calls.filter((call) => call.kind === "stop")).toHaveLength(1);
    await settle();
    moving.resolve(view(true));
    await settle();
    f.advance(1000);
    await settle();
    expect(f.calls.filter((call) => call.kind === "target")).toHaveLength(1);
    expect(f.controller.snapshot().phase).toBe("closed");
  });

  test("a late successful start is stopped/ended after cancellation and never arms", async () => {
    const f = fixture();
    const starting = deferred<ManualView>();
    f.api.start = (body) => {
      f.calls.push({ kind: "start", body });
      return starting.promise;
    };
    await f.controller.refresh();
    await f.controller.review(fixtureLimits());
    const pending = f.controller.start("SYNTHETIC", attest);
    f.controller.revoke("blur");
    expect(f.controller.beginDrag()).toBe(false);
    starting.resolve({
      ...view(true),
      start_request_id: (
        f.calls.find((call) => call.kind === "start")!.body as {
          request_id: string;
        }
      ).request_id,
    });
    await pending;
    expect(f.calls.filter((call) => call.kind === "stop")).toHaveLength(1);
    expect(f.calls.filter((call) => call.kind === "end")).toHaveLength(1);
    expect(f.controller.snapshot().anchor).toBeNull();
    expect(f.controller.snapshot().phase).toBe("closed");
  });

  test("a late challenge cannot create a session after cancellation", async () => {
    const f = fixture();
    const challenge = deferred<{ challenge: string; confirm: string }>();
    f.api.challenge = () => challenge.promise;
    await f.controller.refresh();
    const reviewing = f.controller.review(fixtureLimits());
    f.controller.revoke("hidden");
    challenge.resolve({ challenge: "synthetic", confirm: "SYNTHETIC" });
    await reviewing;
    await f.controller.start("SYNTHETIC", attest);
    expect(f.calls.some((call) => call.kind === "start")).toBe(false);
    expect(f.controller.snapshot().phase).toBe("closed");
  });

  test("API refusal revokes and end sends no manufactured stop evidence", async () => {
    const f = fixture();
    await f.arm();
    f.api.deadman = async () => {
      throw new ManualApiError("reading_stale");
    };
    f.controller.beginDrag();
    await settle();
    expect(f.calls.some((call) => call.kind === "stop")).toBe(true);
    const end = f.calls.find((call) => call.kind === "end")!;
    expect(Object.keys(end.body as object).sort()).toEqual([
      "generation",
      "request_id",
      "session_id",
    ]);
    expect(f.controller.beginDrag()).toBe(false);
  });

  test("unproved stop/end stays fault locked, never a synthetic success", async () => {
    const f = fixture();
    await f.arm();
    f.api.end = async () => {
      throw new ManualApiError("end_refused");
    };
    f.controller.revoke("person_stop");
    await settle();
    expect(f.controller.snapshot().phase).toBe("fault");
    expect(f.controller.snapshot().reason).toBe("end_refused");
    expect(f.controller.beginDrag()).toBe(false);
  });

  test("deadman expiry and disposal close a held stream independently of pending IO", async () => {
    const f = fixture();
    await f.arm();
    const alive = deferred<ManualView>();
    f.api.deadman = () => alive.promise;
    f.controller.beginDrag();
    f.advance(201);
    expect(f.calls.some((call) => call.kind === "stop")).toBe(true);
    f.controller.dispose();
    alive.resolve(view(true));
    await settle();
    expect(f.calls.filter((call) => call.kind === "stop")).toHaveLength(1);
    expect(f.calls.some((call) => call.kind === "target")).toBe(false);
  });
});

describe("manual same-origin HTTP adapter contracts", () => {
  test("strict deadman/gripper/stop/end bodies omit anchor_id while target retains it", async () => {
    const original = globalThis.fetch;
    const sent: {
      path: string;
      init?: RequestInit;
      body: Record<string, unknown>;
    }[] = [];
    let document: unknown = { ...view(), state: "idle", reason_code: null };
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      sent.push({
        path: String(input),
        init,
        body: init?.body ? JSON.parse(String(init.body)) : {},
      });
      return new Response(JSON.stringify(document), { status: 200 });
    }) as typeof fetch;
    try {
      const api = createManualApi();
      const idle = await api.status();
      expect(idle.state).toBe("idle");
      expect(idle.reason_code).toBe("");
      expect(syntheticStartAllowed(idle)).toBe(true);
      document = { ...view(), state: "starting", reason_code: null };
      const preparing = await api.status();
      expect(preparing.state).toBe("starting");
      expect(preparing.anchor).toBeNull();
      expect(syntheticStartAllowed(preparing)).toBe(false);
      document = view(true);
      const id = {
        session_id: "synthetic-session",
        generation: 1,
        anchor_id: "synthetic-anchor",
      };
      await api.deadman({
        ...id,
        request_id: "deadman-request",
        sequence: 1,
        pressed: true,
      });
      await api.target({
        ...id,
        request_id: "target-request",
        sequence: 2,
        delta_position_m: [0.001, 0, 0],
      });
      await api.gripper!({
        ...id,
        request_id: "gripper-request",
        sequence: 3,
        opening_ratio: 0.5,
      });
      await api.stop({ ...id, request_id: "stop-request" });
      await api.end({ ...id, request_id: "end-request" });
      const body = (action: string) =>
        sent.find((entry) => entry.path.endsWith("/" + action))!;
      expect(Object.keys(body("deadman").body).sort()).toEqual([
        "generation",
        "pressed",
        "request_id",
        "sequence",
        "session_id",
      ]);
      expect(Object.keys(body("gripper").body).sort()).toEqual([
        "generation",
        "opening_ratio",
        "request_id",
        "sequence",
        "session_id",
      ]);
      for (const action of ["stop", "end"]) {
        expect(Object.keys(body(action).body).sort()).toEqual([
          "generation",
          "request_id",
          "session_id",
        ]);
      }
      expect(body("target").body.anchor_id).toBe("synthetic-anchor");
      expect(body("target").body.delta_position_m).toEqual([0.001, 0, 0]);
      expect(
        sent.filter((entry) => entry.init?.method === "POST"),
      ).toHaveLength(5);
      expect(
        sent.every((entry) =>
          entry.path.startsWith("/api/levi/marvel0/arm/manual"),
        ),
      ).toBe(true);
    } finally {
      globalThis.fetch = original;
    }
  });

  test("HTTP refusals preserve detail.reason_code ahead of manual_refused", async () => {
    const original = globalThis.fetch;
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({
          detail: {
            code: "manual_refused",
            reason_code: "stop_unproven",
            message: "Manual session refused: stop_unproven",
          },
        }),
        { status: 409 },
      )) as typeof fetch;
    try {
      await expect(createManualApi().status()).rejects.toMatchObject({
        reason: "stop_unproven",
      });
    } finally {
      globalThis.fetch = original;
    }
  });

  test("backend quaternion/orientation boundaries and URDF anchor joint limits are enforced", async () => {
    expect(
      validManualLimits({ ...fixtureLimits(), quaternion_norm_tolerance: 1 }),
    ).toBe(false);
    expect(
      validManualLimits({
        ...fixtureLimits(),
        quaternion_norm_tolerance: 0.999,
      }),
    ).toBe(true);
    expect(
      validManualLimits({
        ...fixtureLimits(),
        max_orientation_drift_rad: Math.PI,
      }),
    ).toBe(true);
    expect(
      validManualLimits({
        ...fixtureLimits(),
        max_orientation_drift_rad: Math.PI + 0.0001,
      }),
    ).toBe(false);
    const bad = view(true);
    bad.anchor!.joints_rad[0] = 99;
    expect(validManualAnchor(bad.anchor)).toBe(false);
    const f = fixture();
    f.api.start = async (body) => ({
      ...bad,
      start_request_id: body.request_id,
    });
    await f.arm();
    expect(f.controller.snapshot().phase).toBe("closed");
    expect(f.controller.snapshot().anchor).toBeNull();
    expect(f.calls.some((call) => call.kind === "stop")).toBe(true);
    expect(f.controller.beginDrag()).toBe(false);
  });
});

/** Public HTTP adapter fixtures only: no runtime hooks, robot endpoint, or motion. */
function lostStartWire(bodyNeverCompletes = false) {
  let document = view();
  let ownStartRequest = "";
  let startSignal: AbortSignal | null = null;
  const delayedFetch = deferred<Response>();
  const delayedBody = deferred<unknown>();
  const calls: { kind: string; body: Record<string, unknown> }[] = [];
  const response = (value: unknown) =>
    new Response(JSON.stringify(value), { status: 200 });
  const fetchMock = (async (input: unknown, init?: RequestInit) => {
    const kind =
      init?.method === "POST" ? String(input).split("/").at(-1)! : "status";
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ kind, body });
    if (kind === "challenge")
      return response({ challenge: "wire-nonce", confirm: "SYNTHETIC" });
    if (kind === "start") {
      ownStartRequest = String(body.request_id);
      startSignal = init?.signal as AbortSignal;
      document = {
        ...view(true),
        state: "starting",
        start_request_id: ownStartRequest,
        anchor_id: null,
        anchor: null,
      };
      // Both loss modes pass through createCaller's real Promise.race deadline.
      if (bodyNeverCompletes)
        return {
          ok: true,
          status: 200,
          json: () => delayedBody.promise,
        } as Response;
      return delayedFetch.promise;
    }
    if (kind === "stop")
      document = { ...document, state: "held", admission: "closed" };
    if (kind === "end")
      document = { ...document, state: "ended", admission: "closed" };
    return response(document);
  }) as typeof fetch;
  return {
    fetchMock,
    calls,
    get document() {
      return document;
    },
    set document(value: ManualView) {
      document = value;
    },
    get requestID() {
      return ownStartRequest;
    },
    get aborted() {
      return startSignal?.aborted === true;
    },
    late() {
      delayedFetch.resolve(
        response({ ...view(true), start_request_id: ownStartRequest }),
      );
      delayedBody.resolve({ ...view(true), start_request_id: ownStartRequest });
    },
  };
}

function phaseReached(
  controller: ManualSessionController,
  phase: string,
): Promise<void> {
  if (controller.snapshot().phase === phase) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let unsubscribe = () => {};
    const timer = setTimeout(() => {
      unsubscribe();
      reject(
        new Error(
          "Expected manual phase " +
            phase +
            ", got " +
            controller.snapshot().phase,
        ),
      );
    }, 1000);
    unsubscribe = controller.subscribe((snapshot) => {
      if (snapshot.phase !== phase) return;
      clearTimeout(timer);
      unsubscribe();
      resolve();
    });
  });
}

describe("real createCaller lost-start recovery and strict ownership", () => {
  for (const bodyNeverCompletes of [false, true]) {
    test(
      bodyNeverCompletes
        ? "body deadline recovers a starting owner without anchor, close only"
        : "fetch deadline recovers a starting owner without anchor, close only",
      async () => {
        const original = globalThis.fetch;
        const wire = lostStartWire(bodyNeverCompletes);
        globalThis.fetch = wire.fetchMock;
        const controller = new ManualSessionController(
          createManualApi({ requestTimeoutMs: 40 }),
        );
        const phases: string[] = [];
        controller.subscribe((snapshot) => phases.push(snapshot.phase));
        try {
          await controller.refresh();
          await controller.review(fixtureLimits());
          await controller.start("SYNTHETIC", attest);
          expect(wire.aborted).toBe(true);
          expect(controller.snapshot().phase).toBe("closed");
          expect(
            phases.some((phase) => phase === "armed" || phase === "holding"),
          ).toBe(false);
          expect(controller.beginDrag()).toBe(false);
          expect(
            wire.calls.filter((call) => call.kind === "status"),
          ).toHaveLength(2);
          for (const kind of ["stop", "end"]) {
            const writes = wire.calls.filter((call) => call.kind === kind);
            expect(writes).toHaveLength(1);
            expect(Object.keys(writes[0].body).sort()).toEqual([
              "generation",
              "request_id",
              "session_id",
            ]);
            expect(writes[0].body.session_id).toBe("synthetic-session");
            expect(writes[0].body.generation).toBe(1);
          }
          wire.late();
          await settle();
          expect(controller.snapshot().phase).toBe("closed");
          expect(
            wire.calls.filter((call) => call.kind === "stop"),
          ).toHaveLength(1);
          expect(
            wire.calls.some((call) =>
              ["target", "deadman"].includes(call.kind),
            ),
          ).toBe(false);
        } finally {
          controller.dispose();
          await controller.refresh();
          globalThis.fetch = original;
        }
      },
    );
  }

  test("unmatched start request stays fault locked across Refresh; explicit own recovery never arms", async () => {
    const original = globalThis.fetch;
    const wire = lostStartWire();
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      const result = await wire.fetchMock(input as string, init);
      if (init?.method !== "POST" && wire.requestID) {
        return new Response(
          JSON.stringify({
            ...wire.document,
            start_request_id: "another-person-request",
          }),
        );
      }
      return result;
    }) as typeof fetch;
    const controller = new ManualSessionController(
      createManualApi({ requestTimeoutMs: 40 }),
    );
    const phases: string[] = [];
    controller.subscribe((snapshot) => phases.push(snapshot.phase));
    try {
      await controller.refresh();
      await controller.review(fixtureLimits());
      await controller.start("SYNTHETIC", attest);
      expect(controller.snapshot().phase).toBe("fault");
      expect(controller.snapshot().reason).toBe("stop_unproven");
      expect(
        wire.calls.some((call) => call.kind === "stop" || call.kind === "end"),
      ).toBe(false);
      wire.document = { ...view(), state: "idle" };
      await controller.refresh();
      expect(controller.snapshot().phase).toBe("fault");
      expect(controller.snapshot().reason).toBe("stop_unproven");
      expect(controller.beginDrag()).toBe(false);
      controller.revoke("person_stop");
      await controller.refresh();
      expect(
        wire.calls.some((call) => call.kind === "stop" || call.kind === "end"),
      ).toBe(false);
      globalThis.fetch = wire.fetchMock;
      wire.document = {
        ...view(true),
        state: "starting",
        start_request_id: wire.requestID,
        anchor_id: null,
        anchor: null,
        environment: "production",
      };
      await controller.refresh();
      expect(controller.snapshot().phase).toBe("fault");
      expect(
        wire.calls.some((call) => call.kind === "stop" || call.kind === "end"),
      ).toBe(false);
      // A later human Stop can close only the original proven synthetic start.
      wire.document = { ...wire.document, environment: "synthetic" };
      controller.revoke("person_stop");
      await controller.refresh();
      expect(controller.snapshot().phase).toBe("closed");
      expect(wire.calls.filter((call) => call.kind === "stop")).toHaveLength(1);
      expect(
        phases.some((phase) => phase === "armed" || phase === "holding"),
      ).toBe(false);
      wire.late();
      await settle();
      expect(controller.beginDrag()).toBe(false);
    } finally {
      controller.dispose();
      await controller.refresh();
      globalThis.fetch = original;
    }
  });

  test("dispose performs one bounded own-start recovery while the start HTTP response is lost", async () => {
    const original = globalThis.fetch;
    const wire = lostStartWire();
    globalThis.fetch = wire.fetchMock;
    const controller = new ManualSessionController(
      createManualApi({ requestTimeoutMs: 40 }),
    );
    const phases: string[] = [];
    controller.subscribe((snapshot) => phases.push(snapshot.phase));
    try {
      await controller.refresh();
      await controller.review(fixtureLimits());
      const pending = controller.start("SYNTHETIC", attest);
      controller.dispose();
      await pending;
      await settle();
      expect(wire.calls.filter((call) => call.kind === "status")).toHaveLength(
        2,
      );
      expect(wire.calls.filter((call) => call.kind === "stop")).toHaveLength(1);
      expect(wire.calls.filter((call) => call.kind === "end")).toHaveLength(1);
      expect(
        phases.some((phase) => phase === "armed" || phase === "holding"),
      ).toBe(false);
      expect(controller.beginDrag()).toBe(false);
      wire.late();
      await settle();
      expect(wire.calls.filter((call) => call.kind === "stop")).toHaveLength(1);
    } finally {
      controller.dispose();
      await controller.refresh();
      globalThis.fetch = original;
    }
  });

  test("wire views require explicit admission and positive-or-null generation", async () => {
    const original = globalThis.fetch;
    let document: Record<string, unknown> = {
      ...view(),
      state: "idle",
      reason_code: null,
    };
    globalThis.fetch = (async () =>
      new Response(JSON.stringify(document))) as typeof fetch;
    try {
      const api = createManualApi();
      expect((await api.status()).reason_code).toBe("");
      for (const field of ["admission", "generation", "start_request_id"]) {
        document = { ...view() };
        delete document[field];
        await expect(api.status()).rejects.toMatchObject({
          reason: "invalid_response",
        });
      }
      for (const invalid of [0, -1, 1.5, "1"]) {
        document = { ...view(), generation: invalid };
        await expect(api.status()).rejects.toMatchObject({
          reason: "invalid_response",
        });
      }
      document = { ...view(), admission: "unknown" };
      await expect(api.status()).rejects.toMatchObject({
        reason: "invalid_response",
      });
      document = {
        ...view(true),
        state: "starting",
        anchor: null,
        anchor_id: null,
        reason_code: null,
        start_request_id: "own-start",
      };
      expect((await api.status()).anchor).toBeNull();
    } finally {
      globalThis.fetch = original;
    }
  });

  for (const [wrongAction, wrongField] of [
    ["stop", "session_id"],
    ["stop", "generation"],
    ["end", "session_id"],
    ["end", "generation"],
  ] as const) {
    test(
      wrongAction +
        " response with a different " +
        wrongField +
        " remains stop_unproven",
      async () => {
        const original = globalThis.fetch;
        let document = view();
        const sent: string[] = [];
        globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
          const action =
            init?.method === "POST"
              ? String(input).split("/").at(-1)!
              : "status";
          sent.push(action);
          if (action === "challenge")
            return new Response(
              JSON.stringify({ challenge: "wire", confirm: "SYNTHETIC" }),
            );
          if (action === "start") {
            const body = JSON.parse(String(init!.body));
            document = { ...view(true), start_request_id: body.request_id };
          }
          if (action === "stop")
            document = { ...document, state: "held", admission: "closed" };
          if (action === "end")
            document = { ...document, state: "ended", admission: "closed" };
          const answer =
            action === wrongAction
              ? {
                  ...document,
                  [wrongField]:
                    wrongField === "session_id" ? "another-person-session" : 9,
                }
              : document;
          return new Response(JSON.stringify(answer));
        }) as typeof fetch;
        const controller = new ManualSessionController(createManualApi());
        try {
          await controller.refresh();
          await controller.review(fixtureLimits());
          await controller.start("SYNTHETIC", attest);
          expect(controller.snapshot().phase).toBe("armed");
          controller.revoke("person_stop");
          await phaseReached(controller, "fault");
          expect(controller.snapshot().phase).toBe("fault");
          expect(controller.snapshot().reason).toBe("stop_unproven");
          if (wrongAction === "stop") expect(sent.includes("end")).toBe(false);
          expect(controller.beginDrag()).toBe(false);
          document = { ...view(), state: "idle" };
          await controller.refresh();
          expect(controller.snapshot().phase).toBe("fault");
          expect(controller.snapshot().reason).toBe("stop_unproven");
        } finally {
          controller.dispose();
          await controller.refresh();
          globalThis.fetch = original;
        }
      },
    );
  }

  test("missing admission on a real stop reply never advances to end", async () => {
    const original = globalThis.fetch;
    let document = view();
    const sent: string[] = [];
    globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
      const action =
        init?.method === "POST" ? String(input).split("/").at(-1)! : "status";
      sent.push(action);
      if (action === "challenge")
        return new Response(
          JSON.stringify({ challenge: "wire", confirm: "SYNTHETIC" }),
        );
      if (action === "start")
        document = {
          ...view(true),
          start_request_id: JSON.parse(String(init!.body)).request_id,
        };
      const answer: Record<string, unknown> = { ...document };
      if (action === "stop") delete answer.admission;
      return new Response(JSON.stringify(answer));
    }) as typeof fetch;
    const controller = new ManualSessionController(createManualApi());
    try {
      await controller.refresh();
      await controller.review(fixtureLimits());
      await controller.start("SYNTHETIC", attest);
      controller.revoke("person_stop");
      await phaseReached(controller, "fault");
      expect(controller.snapshot().phase).toBe("fault");
      expect(sent.includes("end")).toBe(false);
      expect(controller.beginDrag()).toBe(false);
    } finally {
      controller.dispose();
      await controller.refresh();
      globalThis.fetch = original;
    }
  });
});

describe("start response request binding through the real HTTP adapter", () => {
  for (const proveOwnRequest of [false, true]) {
    test(
      proveOwnRequest
        ? "mismatched success never arms; status may close only the original own request"
        : "mismatched success without proof never closes its unrelated owner",
      async () => {
        const original = globalThis.fetch;
        let document = view();
        const writes: { action: string; body: Record<string, unknown> }[] = [];
        const response = (value: unknown) =>
          new Response(JSON.stringify(value));
        globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
          const action =
            init?.method === "POST"
              ? String(input).split("/").at(-1)!
              : "status";
          const body = init?.body ? JSON.parse(String(init.body)) : {};
          if (action !== "status") writes.push({ action, body });
          if (action === "challenge")
            return response({ challenge: "wire", confirm: "SYNTHETIC" });
          if (action === "start") {
            if (proveOwnRequest)
              document = {
                ...view(true),
                state: "starting",
                start_request_id: body.request_id,
                anchor_id: null,
                anchor: null,
              };
            return response({
              ...view(true),
              session_id: "unrelated-owner",
              generation: 9,
              start_request_id: "unrelated-start",
            });
          }
          if (action === "stop")
            document = { ...document, state: "held", admission: "closed" };
          if (action === "end")
            document = { ...document, state: "ended", admission: "closed" };
          return response(document);
        }) as typeof fetch;
        const controller = new ManualSessionController(createManualApi());
        const phases: string[] = [];
        controller.subscribe((snapshot) => phases.push(snapshot.phase));
        try {
          await controller.refresh();
          await controller.review(fixtureLimits());
          await controller.start("SYNTHETIC", attest);
          expect(
            phases.some((phase) => phase === "armed" || phase === "holding"),
          ).toBe(false);
          expect(controller.beginDrag()).toBe(false);
          const cleanup = writes.filter(
            (write) => write.action === "stop" || write.action === "end",
          );
          if (proveOwnRequest) {
            expect(controller.snapshot().phase).toBe("closed");
            expect(cleanup).toHaveLength(2);
            expect(
              cleanup.every(
                (write) =>
                  write.body.session_id === "synthetic-session" &&
                  write.body.generation === 1,
              ),
            ).toBe(true);
          } else {
            expect(cleanup).toHaveLength(0);
            expect(controller.snapshot().phase).toBe("fault");
            expect(controller.snapshot().reason).toBe("stop_unproven");
            await controller.refresh();
            expect(controller.snapshot().phase).toBe("fault");
          }
        } finally {
          controller.dispose();
          await controller.refresh();
          globalThis.fetch = original;
        }
      },
    );
  }
});

describe("production manual capability and stop proof", () => {
  const realView = (active = false): ManualView => ({
    ...view(active),
    environment: "production",
    capabilities: {
      real_motion: true,
      synthetic_only: false,
      can_start: !active,
      gripper: true,
    },
  });
  const stopLimits = {
    settle_window_s: 0.2,
    settle_position_m: 0.001,
    settle_angle_rad: 0.01,
    gripper_settle_rad: 0.01,
  };
  test("only exact production capability tuple permits review; reading sends no writes", async () => {
    expect(manualStartAllowed(realView())).toBe(true);
    expect(
      manualStartAllowed({
        ...realView(),
        capabilities: { ...realView().capabilities, synthetic_only: true },
      }),
    ).toBe(false);
    const f = fixture();
    f.api.status = async () => realView();
    await f.controller.refresh();
    expect(f.calls).toHaveLength(0);
    await f.controller.review(fixtureLimits());
    expect(f.controller.snapshot().reason).toBe("stop_limits_required");
    expect(f.calls).toHaveLength(0);
  });
  test("real confirmation and stop limits bind challenge/start; gripper shares deadman sequence", async () => {
    const f = fixture();
    let requestID = "";
    const active = () => ({ ...realView(true), start_request_id: requestID });
    f.api.status = async () => realView();
    f.api.challenge = async (body) => {
      f.calls.push({ kind: "challenge", body });
      return {
        challenge: "real-nonce",
        confirm: "start real translation session",
      };
    };
    f.api.start = async (body) => {
      requestID = body.request_id;
      f.calls.push({ kind: "start", body });
      return active();
    };
    f.api.deadman = async (body) => {
      f.calls.push({ kind: "deadman", body });
      return active();
    };
    f.api.gripper = async (body) => {
      f.calls.push({ kind: "gripper", body });
      return active();
    };
    f.api.stop = async () => ({
      ...active(),
      admission: "closed",
      state: "held",
      stop_evidence: "admission_closed",
    });
    f.api.end = async () => ({
      ...active(),
      admission: "closed",
      state: "ended",
      stop_evidence: "physical_settled",
    });
    await f.controller.refresh();
    await f.controller.review(fixtureLimits(), stopLimits);
    await f.controller.start("start real translation session", attest);
    expect(f.controller.snapshot().phase).toBe("armed");
    expect(
      (f.calls.find((x) => x.kind === "start")!.body as Record<string, unknown>)
        .stop_limits,
    ).toEqual(stopLimits);
    expect(f.controller.beginGripper(1.1)).toBe(false);
    expect(f.controller.beginGripper(0.4)).toBe(true);
    await settle();
    const commands = f.calls.filter((x) =>
      ["deadman", "gripper"].includes(x.kind),
    );
    expect(commands.map((x) => x.kind)).toEqual(["deadman", "gripper"]);
    expect(
      (commands[1].body as Record<string, unknown>).sequence,
    ).toBeGreaterThan((commands[0].body as { sequence: number }).sequence);
    for (let tick = 0; tick < 4; tick++) {
      f.advance(20);
      await settle();
    }
    f.controller.target([0.9, 0.9, 0.9]);
    expect(f.calls.filter((x) => x.kind === "gripper")).toHaveLength(1);
    expect(f.calls.filter((x) => x.kind === "target")).toHaveLength(0);
    expect(f.calls.filter((x) => x.kind === "deadman")).toHaveLength(5);
    f.controller.revoke("release");
    await settle();
    expect(f.controller.snapshot().phase).toBe("closed");
  });
  test("a synthetic end response cannot prove a real session stopped", async () => {
    const f = fixture();
    let requestID = "";
    f.api.status = async () => realView();
    f.api.challenge = async () => ({
      challenge: "nonce",
      confirm: "start real translation session",
    });
    f.api.start = async (body) => {
      requestID = body.request_id;
      return { ...realView(true), start_request_id: requestID };
    };
    f.api.stop = async () => ({
      ...realView(true),
      admission: "closed",
      state: "held",
    });
    await f.controller.refresh();
    await f.controller.review(fixtureLimits(), stopLimits);
    await f.controller.start("start real translation session", attest);
    f.controller.revoke("release");
    await settle();
    expect(f.controller.snapshot().phase).toBe("fault");
    expect(f.controller.snapshot().reason).toBe("stop_unproven");
    expect(f.controller.beginDrag()).toBe(false);
  });
});
