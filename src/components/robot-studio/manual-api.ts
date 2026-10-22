import { createCaller, type ApiResult } from "@/components/marvel0/deploy-api";
import { jointsWithinLimits } from "@/lib/robot-studio/fr3-kinematics";

export type ManualPoint = [number, number, number];
export type ManualAnchor = {
  position: ManualPoint;
  quaternion: [number, number, number, number];
  joints_rad: [number, number, number, number, number, number, number];
  sampled_at_mono: number;
};
export type ManualLimits = {
  box_min_m: ManualPoint;
  box_max_m: ManualPoint;
  max_delta_m: number;
  max_step_m: number;
  max_velocity_m_s: number;
  period_s: number;
  max_reading_age_s: number;
  max_anchor_age_s: number;
  deadman_timeout_s: number;
  target_timeout_s: number;
  max_orientation_drift_rad: number;
  quaternion_norm_tolerance: number;
  max_settle_age_s: number;
};
export type ManualAttest = {
  estop: boolean;
  scene_reset: boolean;
  area_clear: boolean;
};
export type ManualOwner = {
  session_id: string;
  generation: number;
};
export type ManualIdentity = ManualOwner & { anchor_id: string };
export type ManualView = {
  schema: "levi.marvel0.manual.v1";
  state:
    | "unavailable"
    | "idle"
    | "starting"
    | "owned_idle"
    | "moving"
    | "held"
    | "fault_locked"
    | "ended";
  environment: "synthetic" | "production";
  capabilities: {
    real_motion: boolean;
    synthetic_only: boolean;
    can_start: boolean;
  };
  reason_code: string;
  admission: "open" | "closed";
  session_id: string | null;
  start_request_id: string | null;
  generation: number | null;
  anchor_id: string | null;
  anchor: ManualAnchor | null;
  counters: Record<string, unknown>;
  stop_evidence: unknown;
};
export type ManualChallenge = { challenge: string; confirm: string };
export type ManualCommand = ManualIdentity & {
  request_id: string;
  sequence: number;
};

export interface ManualApi {
  status(signal?: AbortSignal): Promise<ManualView>;
  challenge(body: { limits: ManualLimits }): Promise<ManualChallenge>;
  start(body: {
    request_id: string;
    limits: ManualLimits;
    challenge: string;
    confirm: string;
    attest: ManualAttest;
  }): Promise<ManualView>;
  target(
    body: ManualCommand & { delta_position_m: ManualPoint },
    signal?: AbortSignal,
  ): Promise<ManualView>;
  deadman(
    body: ManualCommand & { pressed: boolean },
    signal?: AbortSignal,
  ): Promise<ManualView>;
  stop(body: ManualOwner & { request_id: string }): Promise<ManualView>;
  end(body: ManualOwner & { request_id: string }): Promise<ManualView>;
}

export class ManualApiError extends Error {
  constructor(
    public readonly reason: string,
    message = reason,
  ) {
    super(message);
    this.name = "ManualApiError";
  }
}
const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const tuple = (value: unknown, count: number): value is number[] =>
  Array.isArray(value) &&
  value.length === count &&
  Array.from(value).every((v) => typeof v === "number" && Number.isFinite(v));

export function validManualAnchor(value: unknown): value is ManualAnchor {
  return (
    record(value) &&
    tuple(value.position, 3) &&
    tuple(value.quaternion, 4) &&
    Math.abs(Math.hypot(...value.quaternion) - 1) <= 0.001 &&
    tuple(value.joints_rad, 7) &&
    jointsWithinLimits(value.joints_rad) &&
    typeof value.sampled_at_mono === "number" &&
    Number.isFinite(value.sampled_at_mono) &&
    value.sampled_at_mono >= 0
  );
}

function viewOf(value: unknown): ManualView {
  if (
    !record(value) ||
    value.schema !== "levi.marvel0.manual.v1" ||
    ![
      "unavailable",
      "idle",
      "starting",
      "owned_idle",
      "moving",
      "held",
      "fault_locked",
      "ended",
    ].includes(String(value.state)) ||
    !record(value.capabilities) ||
    typeof value.capabilities.can_start !== "boolean" ||
    typeof value.capabilities.real_motion !== "boolean" ||
    typeof value.capabilities.synthetic_only !== "boolean" ||
    (typeof value.reason_code !== "string" && value.reason_code !== null) ||
    (value.admission !== "open" && value.admission !== "closed") ||
    (value.session_id !== null &&
      (typeof value.session_id !== "string" || !value.session_id)) ||
    (value.start_request_id !== null &&
      (typeof value.start_request_id !== "string" ||
        !value.start_request_id)) ||
    (value.generation !== null &&
      (!Number.isInteger(value.generation) || Number(value.generation) <= 0))
  ) {
    throw new ManualApiError("invalid_response");
  }
  return {
    schema: "levi.marvel0.manual.v1",
    state: value.state as ManualView["state"],
    // Missing/unknown environment never grants a synthetic transport.
    environment: value.environment === "synthetic" ? "synthetic" : "production",
    capabilities: {
      real_motion: value.capabilities.real_motion,
      synthetic_only: value.capabilities.synthetic_only,
      can_start: value.capabilities.can_start,
    },
    reason_code: typeof value.reason_code === "string" ? value.reason_code : "",
    admission: value.admission,
    session_id: typeof value.session_id === "string" ? value.session_id : null,
    start_request_id: value.start_request_id as string | null,
    generation: value.generation as number | null,
    anchor_id: typeof value.anchor_id === "string" ? value.anchor_id : null,
    anchor: validManualAnchor(value.anchor) ? value.anchor : null,
    counters: record(value.counters) ? value.counters : {},
    stop_evidence: value.stop_evidence ?? null,
  };
}

function answer(result: ApiResult): unknown {
  if (result.ok) return result.data;
  const reason =
    typeof result.extra.reason_code === "string"
      ? result.extra.reason_code
      : result.code;
  throw new ManualApiError(reason, result.message);
}

/** Same-origin proxy/auth machinery; no credential access or robot-side URL.
 * HTTP deadlines bound transport only and are not physical operating limits. */
export function createManualApi({
  requestTimeoutMs = 5000,
}: { requestTimeoutMs?: number } = {}): ManualApi {
  if (!Number.isFinite(requestTimeoutMs) || requestTimeoutMs <= 0)
    throw new Error("requestTimeoutMs must be positive and finite");
  const call = createCaller();
  const path = "arm/manual";
  const get = async (signal?: AbortSignal) =>
    viewOf(answer(await call(path, requestTimeoutMs, undefined, signal)));
  const post = async (action: string, body: unknown, signal?: AbortSignal) =>
    viewOf(
      answer(await call(path + "/" + action, requestTimeoutMs, body, signal)),
    );
  return {
    status: get,
    challenge: async (body) => {
      const value = answer(
        await call(path + "/challenge", requestTimeoutMs, body),
      );
      if (
        !record(value) ||
        typeof value.challenge !== "string" ||
        !value.challenge ||
        typeof value.confirm !== "string" ||
        !value.confirm
      )
        throw new ManualApiError("invalid_response");
      return { challenge: value.challenge, confirm: value.confirm };
    },
    start: (body) => post("start", body),
    target: (body, signal) => post("target", body, signal),
    deadman: (body, signal) =>
      post(
        "deadman",
        {
          request_id: body.request_id,
          session_id: body.session_id,
          generation: body.generation,
          sequence: body.sequence,
          pressed: body.pressed,
        },
        signal,
      ),
    stop: (body) =>
      post("stop", {
        request_id: body.request_id,
        session_id: body.session_id,
        generation: body.generation,
      }),
    end: (body) =>
      post("end", {
        request_id: body.request_id,
        session_id: body.session_id,
        generation: body.generation,
      }),
  };
}
export const manualApi = createManualApi();
