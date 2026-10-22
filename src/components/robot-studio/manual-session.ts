import { forwardPose } from "@/lib/robot-studio/fr3-kinematics";
import {
  ManualApiError,
  validManualAnchor,
  type ManualAnchor,
  type ManualApi,
  type ManualAttest,
  type ManualChallenge,
  type ManualIdentity,
  type ManualOwner,
  type ManualLimits,
  type ManualPoint,
  type ManualView,
} from "./manual-api";

export type ManualDragController = {
  beginDrag(): boolean;
  target(position: ManualPoint): void;
  revoke(reason: string): void;
};
export type ManualPhase =
  | "unavailable"
  | "ready"
  | "review"
  | "starting"
  | "armed"
  | "holding"
  | "closing"
  | "closed"
  | "fault";
export type ManualSnapshot = {
  phase: ManualPhase;
  reason: string;
  view: ManualView | null;
  anchor: ManualAnchor | null;
  confirm: string | null;
};
type Clock = {
  now(): number;
  schedule(callback: () => void, delayMs: number): unknown;
  cancel(handle: unknown): void;
  requestId(): string;
};
const defaultClock: Clock = {
  now: () => performance.now(),
  schedule: (callback, delay) => setTimeout(callback, delay),
  cancel: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  requestId: () => crypto.randomUUID(),
};
const point = (value: unknown): value is ManualPoint =>
  Array.isArray(value) &&
  value.length === 3 &&
  Array.from(value).every((v) => typeof v === "number" && Number.isFinite(v));

export function validManualLimits(
  value: ManualLimits | null,
): value is ManualLimits {
  if (!value || !point(value.box_min_m) || !point(value.box_max_m))
    return false;
  if (!value.box_min_m.every((v, i) => v < value.box_max_m[i])) return false;
  const keys = [
    "max_delta_m",
    "max_step_m",
    "max_velocity_m_s",
    "period_s",
    "max_reading_age_s",
    "max_anchor_age_s",
    "deadman_timeout_s",
    "target_timeout_s",
    "max_orientation_drift_rad",
    "quaternion_norm_tolerance",
    "max_settle_age_s",
  ] as const;
  return (
    keys.every(
      (key) =>
        typeof value[key] === "number" &&
        Number.isFinite(value[key]) &&
        value[key] > 0,
    ) &&
    value.quaternion_norm_tolerance < 1 &&
    value.max_orientation_drift_rad <= Math.PI
  );
}
export function syntheticStartAllowed(view: ManualView | null): boolean {
  return (
    !!view &&
    ["idle", "ended"].includes(view.state) &&
    view.environment === "synthetic" &&
    view.capabilities.synthetic_only === true &&
    view.capabilities.real_motion === false &&
    view.capabilities.can_start === true
  );
}
const reasonOf = (error: unknown) =>
  error instanceof ManualApiError ? error.reason : "api_failed";
function ownerOnly(id: ManualOwner): ManualOwner {
  return { session_id: id.session_id, generation: id.generation };
}
function ownerOf(view: ManualView): ManualOwner | null {
  return typeof view.session_id === "string" &&
    view.session_id.length > 0 &&
    Number.isInteger(view.generation) &&
    (view.generation ?? 0) > 0
    ? { session_id: view.session_id, generation: view.generation! }
    : null;
}
const sameOwner = (view: ManualView, id: ManualOwner) =>
  view.session_id === id.session_id && view.generation === id.generation;
const syntheticView = (view: ManualView) =>
  view.environment === "synthetic" &&
  view.capabilities.synthetic_only === true &&
  view.capabilities.real_motion === false;
function matchingStartOwner(
  view: ManualView,
  requestID: string,
): ManualOwner | null {
  return syntheticView(view) && view.start_request_id === requestID
    ? ownerOf(view)
    : null;
}
function identity(view: ManualView): ManualIdentity | null {
  const owner = ownerOf(view);
  return owner &&
    typeof view.anchor_id === "string" &&
    view.anchor_id.length > 0
    ? { ...owner, anchor_id: view.anchor_id }
    : null;
}
const sameIdentity = (view: ManualView, id: ManualIdentity) =>
  view.session_id === id.session_id &&
  view.generation === id.generation &&
  view.anchor_id === id.anchor_id;
function cloneAnchor(anchor: ManualAnchor): ManualAnchor {
  return {
    position: [...anchor.position],
    quaternion: [...anchor.quaternion],
    joints_rad: [...anchor.joints_rad],
    sampled_at_mono: anchor.sampled_at_mono,
  };
}

/** DOM-independent lifecycle; an API acknowledgement never proves physical stop.
 * The only executable transport admitted here is explicitly synthetic. */
export class ManualSessionController implements ManualDragController {
  private state: ManualSnapshot = {
    phase: "unavailable",
    reason: "",
    view: null,
    anchor: null,
    confirm: null,
  };
  private listeners = new Set<(snapshot: ManualSnapshot) => void>();
  private clock: Clock;
  private epoch = 0;
  private disposed = false;
  private permission = false;
  private challenge: ManualChallenge | null = null;
  private limits: ManualLimits | null = null;
  private owner: ManualIdentity | null = null;
  private origin: ManualPoint | null = null;
  private desired: ManualPoint = [0, 0, 0];
  private timer: unknown = null;
  private inFlight = false;
  private sequence = 0;
  private startPending = false;
  private pendingStartRequestID: string | null = null;
  private uncertainStart = false;
  private recovery: Promise<void> | null = null;
  private closedStartRequests = new Set<string>();
  private lastDeadman = 0;
  private request: AbortController | null = null;
  private closing = new Map<string, Promise<void>>();

  constructor(
    private api: ManualApi,
    clock: Partial<Clock> = {},
  ) {
    this.clock = { ...defaultClock, ...clock };
  }
  snapshot(): ManualSnapshot {
    return this.state;
  }
  subscribe(listener: (snapshot: ManualSnapshot) => void): () => void {
    this.listeners.add(listener);
    listener(this.state);
    return () => {
      this.listeners.delete(listener);
    };
  }
  private emit(next: Partial<ManualSnapshot>) {
    this.state = { ...this.state, ...next };
    if (!this.disposed)
      this.listeners.forEach((listener) => listener(this.state));
  }
  async refresh(): Promise<void> {
    if (this.pendingStartRequestID && this.uncertainStart) {
      await this.recoverUnknownStart();
      return;
    }
    const epoch = this.epoch;
    try {
      const view = await this.api.status();
      if (this.disposed || epoch !== this.epoch) return;
      this.emit({ view });
      if (
        ["unavailable", "ready", "closed", "fault"].includes(this.state.phase)
      ) {
        this.permission = syntheticStartAllowed(view);
        this.emit({
          phase: this.permission ? "ready" : "unavailable",
          reason: view.reason_code,
        });
      }
    } catch (error) {
      if (!this.disposed && epoch === this.epoch) {
        this.permission = false;
        this.emit({ phase: "unavailable", reason: reasonOf(error) });
      }
    }
  }
  async review(limits: ManualLimits | null): Promise<void> {
    if (
      this.disposed ||
      !this.permission ||
      !syntheticStartAllowed(this.state.view) ||
      !["ready"].includes(this.state.phase)
    )
      return;
    if (!validManualLimits(limits)) {
      this.emit({ reason: "limits_required" });
      return;
    }
    const epoch = ++this.epoch;
    this.limits = {
      ...limits,
      box_min_m: [...limits.box_min_m],
      box_max_m: [...limits.box_max_m],
    };
    this.emit({ phase: "starting", reason: "", confirm: null });
    try {
      const challenge = await this.api.challenge({ limits: this.limits });
      if (this.disposed || epoch !== this.epoch) return;
      this.challenge = challenge;
      this.emit({ phase: "review", confirm: challenge.confirm });
    } catch (error) {
      if (epoch === this.epoch && !this.disposed)
        this.emit({ phase: "fault", reason: reasonOf(error) });
    }
  }
  async start(confirm: string, attest: ManualAttest): Promise<void> {
    if (
      this.disposed ||
      this.state.phase !== "review" ||
      !this.challenge ||
      !this.limits
    )
      return;
    if (
      confirm !== this.challenge.confirm ||
      !attest.estop ||
      !attest.scene_reset ||
      !attest.area_clear
    ) {
      this.emit({ reason: "confirmation_required" });
      return;
    }
    const epoch = ++this.epoch;
    const limits = this.limits;
    const challenge = this.challenge;
    const startRequestID = this.clock.requestId();
    this.pendingStartRequestID = startRequestID;
    this.uncertainStart = false;
    this.permission = false;
    this.startPending = true;
    this.emit({ phase: "starting", reason: "", confirm: null });
    let returned: ManualView | null = null;
    try {
      returned = await this.api.start({
        request_id: startRequestID,
        limits,
        challenge: challenge.challenge,
        confirm,
        attest: { ...attest },
      });
      if (this.closedStartRequests.has(startRequestID)) return;
      const own = matchingStartOwner(returned, startRequestID);
      if (this.disposed || epoch !== this.epoch) {
        this.uncertainStart = true;
        if (own) await this.closeIdentity(own, this.epoch, startRequestID);
        else await this.recoverUnknownStart();
        return;
      }
      const id = identity(returned);
      if (
        !own ||
        !id ||
        returned.admission !== "open" ||
        returned.state !== "owned_idle" ||
        !validManualAnchor(returned.anchor)
      )
        throw new ManualApiError("invalid_anchor");
      const origin = forwardPose(returned.anchor.joints_rad).position;
      this.owner = id;
      this.origin = [...origin];
      this.desired = [0, 0, 0];
      this.sequence = 0;
      this.emit({
        phase: "armed",
        view: returned,
        anchor: cloneAnchor(returned.anchor),
        reason: "",
      });
    } catch {
      if (this.closedStartRequests.has(startRequestID)) return;
      this.uncertainStart = true;
      this.permission = false;
      this.emit({
        phase: "fault",
        anchor: null,
        reason: "stop_unproven",
        confirm: null,
      });
      const own = returned
        ? matchingStartOwner(returned, startRequestID)
        : null;
      if (own) await this.closeIdentity(own, this.epoch, startRequestID);
      else await this.recoverUnknownStart();
    } finally {
      this.startPending = false;
    }
  }
  beginDrag(): boolean {
    if (
      this.disposed ||
      this.state.phase !== "armed" ||
      !this.owner ||
      !this.origin ||
      !this.limits
    )
      return false;
    this.lastDeadman = this.clock.now();
    this.desired = [0, 0, 0];
    this.emit({ phase: "holding", reason: "" });
    void this.pump();
    this.schedule();
    return true;
  }
  target(position: ManualPoint): void {
    if (this.state.phase !== "holding" || !this.origin || !this.limits) return;
    const delta = point(position)
      ? (position.map((v, i) => v - this.origin![i]) as ManualPoint)
      : null;
    if (
      !delta ||
      Math.hypot(...delta) > this.limits.max_delta_m ||
      !position.every(
        (v, i) =>
          v >= this.limits!.box_min_m[i] && v <= this.limits!.box_max_m[i],
      )
    ) {
      this.revoke("target_rejected");
      return;
    }
    // One latest desired target; there is never a queued pointer-event history.
    this.desired = [...delta];
  }
  private schedule() {
    if (this.state.phase !== "holding" || !this.limits) return;
    this.timer = this.clock.schedule(() => {
      this.timer = null;
      if (this.state.phase !== "holding" || !this.limits) return;
      if (
        this.clock.now() - this.lastDeadman >=
        this.limits.deadman_timeout_s * 1000
      ) {
        this.revoke("deadman_expired");
        return;
      }
      void this.pump();
      this.schedule();
    }, this.limits.period_s * 1000);
  }
  private accepted(view: ManualView, id: ManualIdentity) {
    if (
      !sameIdentity(view, id) ||
      view.environment !== "synthetic" ||
      view.capabilities.real_motion !== false ||
      view.capabilities.synthetic_only !== true ||
      view.admission !== "open" ||
      !["owned_idle", "moving"].includes(view.state)
    )
      throw new ManualApiError(view.reason_code || "session_closed");
    this.emit({ view });
  }
  private async pump() {
    if (this.inFlight || this.state.phase !== "holding" || !this.owner) return;
    const epoch = this.epoch;
    const id = { ...this.owner };
    const request = new AbortController();
    this.request = request;
    this.inFlight = true;
    try {
      const alive = await this.api.deadman(
        {
          ...id,
          request_id: this.clock.requestId(),
          sequence: ++this.sequence,
          pressed: true,
        },
        request.signal,
      );
      if (epoch !== this.epoch || this.state.phase !== "holding") return;
      this.accepted(alive, id);
      this.lastDeadman = this.clock.now();
      const position = [...this.desired] as ManualPoint;
      const moved = await this.api.target(
        {
          ...id,
          request_id: this.clock.requestId(),
          sequence: ++this.sequence,
          delta_position_m: position,
        },
        request.signal,
      );
      if (epoch !== this.epoch || this.state.phase !== "holding") return;
      this.accepted(moved, id);
    } catch (error) {
      if (epoch === this.epoch && this.state.phase === "holding")
        this.revoke(reasonOf(error));
    } finally {
      if (this.request === request) {
        this.request = null;
        this.inFlight = false;
      }
    }
  }
  revoke(reason: string): void {
    if (this.state.phase === "closing") return;
    if (this.pendingStartRequestID && this.uncertainStart) {
      this.permission = false;
      this.emit({ phase: "fault", reason: "stop_unproven", anchor: null });
      if (reason === "person_stop") void this.recoverUnknownStart();
      return;
    }
    const pendingStart = this.startPending;
    const id = this.owner;
    const epoch = ++this.epoch;
    this.permission = false;
    this.owner = null;
    this.origin = null;
    this.desired = [0, 0, 0];
    this.challenge = null;
    if (this.timer !== null) this.clock.cancel(this.timer);
    this.timer = null;
    this.request?.abort();
    this.request = null;
    this.inFlight = false;
    this.uncertainStart = !!this.pendingStartRequestID;
    this.emit({
      phase: id || pendingStart ? "closing" : "closed",
      reason,
      anchor: null,
      confirm: null,
    });
    if (id)
      void this.closeIdentity(ownerOnly(id), epoch, this.pendingStartRequestID);
    // A pending start will be handled by its response or timeout recovery.
  }

  /** One bounded GET per recovery request, no polling/retry loop. An unmatched
   * status cannot erase the unknown-start latch or grant a fresh session. */
  private recoverUnknownStart(): Promise<void> {
    const wanted = this.pendingStartRequestID;
    if (!wanted) return Promise.resolve();
    if (this.recovery) return this.recovery;
    this.permission = false;
    this.uncertainStart = true;
    this.emit({
      phase: "fault",
      reason: "stop_unproven",
      anchor: null,
      confirm: null,
    });
    const epoch = this.epoch;
    this.recovery = (async () => {
      try {
        // The real ManualApi status uses createCaller's bounded HTTP deadline.
        const view = await Promise.resolve().then(() => this.api.status());
        if (this.pendingStartRequestID !== wanted) return;
        this.emit({ view });
        const own = matchingStartOwner(view, wanted);
        if (!own) {
          this.emit({ phase: "fault", reason: "stop_unproven", anchor: null });
          return;
        }
        await this.closeIdentity(own, epoch, wanted);
      } catch {
        if (this.pendingStartRequestID === wanted)
          this.emit({ phase: "fault", reason: "stop_unproven", anchor: null });
      } finally {
        this.recovery = null;
      }
    })();
    return this.recovery;
  }

  private closeIdentity(
    id: ManualOwner,
    epoch: number,
    startRequestID: string | null,
  ): Promise<void> {
    if (startRequestID && this.closedStartRequests.has(startRequestID))
      return Promise.resolve();
    const key = id.session_id + "/" + id.generation;
    const existing = this.closing.get(key);
    if (existing) return existing;
    const close = (async () => {
      try {
        const stopped = await this.api.stop({
          ...id,
          request_id: this.clock.requestId(),
        });
        if (
          !sameOwner(stopped, id) ||
          !syntheticView(stopped) ||
          stopped.admission !== "closed"
        )
          throw new ManualApiError("stop_unproven");
        const ended = await this.api.end({
          ...id,
          request_id: this.clock.requestId(),
        });
        if (
          !sameOwner(ended, id) ||
          !syntheticView(ended) ||
          ended.state !== "ended" ||
          ended.admission !== "closed"
        )
          throw new ManualApiError("stop_unproven");
        if (startRequestID) this.closedStartRequests.add(startRequestID);
        if (this.pendingStartRequestID === startRequestID) {
          this.pendingStartRequestID = null;
          this.uncertainStart = false;
        }
        if (epoch === this.epoch)
          this.emit({
            phase: "closed",
            view: ended,
            anchor: null,
            confirm: null,
            reason: ended.reason_code,
          });
      } catch (error) {
        if (this.pendingStartRequestID) this.uncertainStart = true;
        if (epoch === this.epoch)
          this.emit({ phase: "fault", reason: reasonOf(error), anchor: null });
      }
    })();
    this.closing.set(key, close);
    void close.then(() => this.closing.delete(key));
    return close;
  }

  dispose(): void {
    if (this.disposed) return;
    this.revoke("unmount");
    this.disposed = true;
    this.listeners.clear();
    // Once on disposal, even if a prior automatic query could not prove owner.
    // In-flight recovery is shared; never keep a background retry loop alive.
    if (this.pendingStartRequestID && !this.owner)
      void this.recoverUnknownStart();
  }
}
