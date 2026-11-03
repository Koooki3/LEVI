// Typed client of the arm stack routes (`/api/levi/marvel0/stack...`, levi/
// marvel0/stack_act_api.py): the state, a start or stop per terminal, the
// marker, the two checks, the gripper test, the guided sequence and a
// terminal's log. Same origin: the proxy adds the person's token, nothing here
// reads one. A write carries a `request_id` the caller keeps across a retry.
// Nothing here talks to a robot-side port.
import { createCaller, type ApiResult } from "./deploy-api";
import { lower, type CompId } from "./stack-act-logic";
import { MARVEL0_REQUEST_MS } from "./marvel0-logic";

export interface StackDeadlines {
  read: number;
  /** A start waits for the backend's checks and `systemd-run`. */
  start: number;
  /** A stop waits for the unit to go (its stop timeout plus margin). */
  stop: number;
  /** A check or the gripper test runs a short job and reads its result. */
  check: number;
}
export const STACK_DEADLINES: StackDeadlines = {
  read: MARVEL0_REQUEST_MS,
  start: 90_000,
  stop: 240_000,
  check: 60_000,
};

export type MarkerOp = "touch" | "release" | "adopt";
export type GripperAction = "open" | "close";

export interface StackApi {
  read(signal?: AbortSignal): Promise<ApiResult>;
  start(id: CompId, body: unknown): Promise<ApiResult>;
  stop(id: CompId, body: unknown): Promise<ApiResult>;
  marker(body: unknown): Promise<ApiResult>;
  /** Chooses the arm controller A starts as next (only while A is stopped),
   * or goes back to the control file's default. */
  armChoice(body: unknown): Promise<ApiResult>;
  /** A's FCI check (`a`) or the read-only checks before C (`c`). */
  preflight(id: "A" | "C", body: unknown): Promise<ApiResult>;
  gripper(body: unknown): Promise<ApiResult>;
  sequence(body: unknown): Promise<ApiResult>;
  /** Stops the whole stack: C, then A, then B (M stays). */
  sequenceStop(body: unknown): Promise<ApiResult>;
  /** Ends the guided start or stop; nothing that already runs is stopped. */
  sequenceCancel(body: unknown): Promise<ApiResult>;
  log(id: CompId, lines: number, signal?: AbortSignal): Promise<ApiResult>;
}

export function createStackApi({
  fetchFn,
  deadlines = {},
}: {
  fetchFn?: (input: string, init?: RequestInit) => Promise<Response>;
  deadlines?: Partial<StackDeadlines>;
} = {}): StackApi {
  const limit = { ...STACK_DEADLINES, ...deadlines };
  const call = createCaller({ fetchFn });
  return {
    read: (signal) => call("stack", limit.read, undefined, signal),
    start: (id, body) => call(`stack/${lower(id)}/start`, limit.start, body),
    stop: (id, body) => call(`stack/${lower(id)}/stop`, limit.stop, body),
    marker: (body) => call("stack/marker", limit.check, body),
    armChoice: (body) => call("stack/a/controller", limit.check, body),
    preflight: (id, body) =>
      call(`stack/${lower(id)}/preflight`, limit.check, body),
    gripper: (body) => call("stack/b/gripper", limit.check, body),
    sequence: (body) => call("stack/sequence/start", limit.start, body),
    // Only begins the sequence and answers; the three stops run on in the
    // backend and the page follows them in the state.
    sequenceStop: (body) => call("stack/sequence/stop", limit.start, body),
    sequenceCancel: (body) => call("stack/sequence/cancel", limit.check, body),
    log: (id, lines, signal) =>
      call(
        `stack/${lower(id)}/log?lines=${Math.trunc(lines)}`,
        limit.read,
        undefined,
        signal,
      ),
  };
}

/** The client the page uses. */
export const stackApi: StackApi = createStackApi();

// -- request bodies (closed: the backend refuses a key it does not know)

/** The arm controller A's start window named, as the backend takes it
 * (`ArmNamed`); sent only with A's start and the whole stack's. */
export interface NamedArm {
  controller: string;
  preset: string | null;
}
const armField = (arm: NamedArm | null | undefined) =>
  arm ? { arm: { controller: arm.controller, preset: arm.preset } } : {};

/** The body of a single start. `arm` (A only): the controller the window
 * named; B's and C's closed bodies have no such key, so it is left out. */
export const startBody = (
  requestId: string,
  confirm: string,
  attest: Record<string, boolean>,
  arm: NamedArm | null = null,
): Record<string, unknown> => ({
  request_id: requestId,
  confirm,
  attest,
  ...armField(arm),
});

export const stopBody = (
  requestId: string,
  confirm: string,
  clientOverride: string,
): Record<string, unknown> => ({
  request_id: requestId,
  confirm,
  client_override: clientOverride,
});

export const markerBody = (
  requestId: string,
  op: MarkerOp,
  confirm: string,
): Record<string, unknown> => ({ request_id: requestId, op, confirm });

/** The body of A's controller choice (`ArmChoiceBody` of the backend):
 * `controller` is `lab`, `anyspeedup` (with its `preset`) or `default`. */
export const armChoiceBody = (
  requestId: string,
  pick: { controller: string; preset: string | null },
): Record<string, unknown> => ({
  request_id: requestId,
  controller: pick.controller,
  preset: pick.preset,
});

export const preflightBody = (requestId: string): Record<string, unknown> => ({
  request_id: requestId,
});

export const gripperBody = (
  requestId: string,
  action: GripperAction,
  confirm: string,
  attest: Record<string, boolean>,
): Record<string, unknown> => ({
  request_id: requestId,
  action,
  confirm,
  attest,
});

/** The body of the start of the whole stack (`SequenceStartBody` of the
 * backend): the checks of A, B and C, `through: "c"` (all the way to C on this
 * one confirmation), the two pauses, both off unless the person switched
 * the pause on, and `arm`, the controller the window named for A. */
export const sequenceBody = (
  requestId: string,
  confirm: string,
  attest: Record<string, boolean>,
  pause = false,
  arm: NamedArm | null = null,
): Record<string, unknown> => ({
  request_id: requestId,
  confirm,
  attest,
  through: "c",
  pause_before_b: pause,
  pause_before_c: pause,
  ...armField(arm),
});

/** The body of the stop of the whole stack: the same closed shape as a
 * single start's (a stop ticks nothing). */
export const sequenceStopBody = (
  requestId: string,
  confirm: string,
  attest: Record<string, boolean>,
): Record<string, unknown> => ({ request_id: requestId, confirm, attest });
