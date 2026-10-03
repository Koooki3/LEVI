// What the navigation needs to know about the live service, and nothing more.
//
//   * The first request asks `/api/levi/live/status` whether this LEVI is the
//     live annotation workspace at all (`enabled`). In any other workspace
//     (the one a person works in every day) that is the only request this
//     module ever makes: no timer, no polling.
//   * In a live workspace it refreshes the same lightweight status every
//     8 s, not while the tab is hidden, with a growing wait after failures.
//   * One store per page load, shared by the header and the home banner.
import type { LiveStatusResponse } from "./types";

export const PULSE_POLL_MS = 8000;
export const PULSE_MAX_MS = 30000;

export interface PulseState {
  /** null until the first answer (or while the core does not answer). */
  enabled: boolean | null;
  status: LiveStatusResponse | null;
  /** Failed requests in a row. */
  failures: number;
  /** Epoch milliseconds of the last good answer. */
  at: number | null;
}

export interface PulseDeps {
  fetchStatus: (signal: AbortSignal) => Promise<LiveStatusResponse>;
  visible: () => boolean;
  setTimer: (fn: () => void, ms: number) => unknown;
  clearTimer: (handle: unknown) => void;
  now: () => number;
}

/** Milliseconds to the next request after `failures` failures in a row. */
export function pulseDelay(failures: number): number {
  if (failures <= 0) return PULSE_POLL_MS;
  return Math.min(PULSE_MAX_MS, PULSE_POLL_MS * 2 ** Math.min(failures, 4));
}

export class PulseStore {
  private state: PulseState = {
    enabled: null,
    status: null,
    failures: 0,
    at: null,
  };
  private listeners = new Set<() => void>();
  private timer: unknown = null;
  private controller: AbortController | null = null;
  private running = false;

  constructor(private deps: PulseDeps) {}

  getSnapshot = (): PulseState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    if (this.listeners.size === 1) this.start();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.stop();
    };
  };

  /** The tab was hidden or shown (the page wires `visibilitychange` to it). */
  visibilityChanged() {
    // Not the live workspace: nothing to ask, not even on coming back to the tab.
    if (!this.running || this.state.enabled === false) return;
    this.clear();
    if (this.deps.visible()) void this.tick();
    else this.controller?.abort();
  }

  private set(next: PulseState) {
    this.state = next;
    for (const listener of [...this.listeners]) listener();
  }

  private clear() {
    if (this.timer != null) this.deps.clearTimer(this.timer);
    this.timer = null;
  }

  private start() {
    this.running = true;
    // A live workspace is known for good once answered; any other too.
    if (this.state.enabled === false) return;
    if (this.deps.visible()) void this.tick();
  }

  private stop() {
    this.running = false;
    this.clear();
    this.controller?.abort();
  }

  private schedule() {
    this.clear();
    // Not live: nothing more to ask, ever (until the page is reloaded).
    if (!this.running || this.state.enabled === false) return;
    if (!this.deps.visible()) return;
    this.timer = this.deps.setTimer(
      () => void this.tick(),
      pulseDelay(this.state.failures),
    );
  }

  private async tick() {
    if (!this.running) return;
    this.controller = new AbortController();
    const signal = this.controller.signal;
    try {
      const status = await this.deps.fetchStatus(signal);
      if (!this.running || signal.aborted) return;
      this.set({
        enabled: status.enabled === true,
        status: status.enabled === true ? status : null,
        failures: 0,
        at: this.deps.now(),
      });
    } catch {
      if (!this.running || signal.aborted) return;
      this.set({ ...this.state, failures: this.state.failures + 1 });
    }
    this.schedule();
  }
}

async function fetchStatus(signal: AbortSignal): Promise<LiveStatusResponse> {
  const response = await fetch("/api/levi/live/status", {
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

let shared: PulseStore | null = null;

/** The page's one store (browser only). */
export function pulseStore(): PulseStore {
  if (!shared) {
    shared = new PulseStore({
      fetchStatus,
      visible: () => document.visibilityState !== "hidden",
      setTimer: (fn, ms) => setTimeout(fn, ms),
      clearTimer: (handle) => clearTimeout(handle as number),
      now: () => Date.now(),
    });
    document.addEventListener("visibilitychange", () =>
      shared?.visibilityChanged(),
    );
  }
  return shared;
}
