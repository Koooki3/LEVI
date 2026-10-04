// What the navigation needs to know about the live service, and nothing more.
//
//   * The first request asks `/api/levi/live/status` whether this LEVI shows a
//     live annotation service at all (`enabled`): the live workspace's own
//     core, or the product LEVI once it finds the live workspace
//     (levi/live/locate.py). Without one it asks again once a minute (a
//     service started later appears without a reload), never while the tab
//     is hidden.
//   * With one it refreshes the same lightweight status every 8 s, not while
//     the tab is hidden, with a growing wait after failures.
//   * One store per page load, shared by the header and the home banner.
import type { LiveStatusResponse } from "./types";

export const PULSE_POLL_MS = 8000;
export const PULSE_MAX_MS = 30000;
/** How often a LEVI without a live service asks whether one appeared. */
export const PULSE_OFF_MS = 60000;

export interface PulseState {
  /** null until the first answer (or while the core does not answer). */
  enabled: boolean | null;
  /** true when this LEVI is not the live workspace itself but shows it (the
   * product LEVI); null until known. */
  embedded: boolean | null;
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
    embedded: null,
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
    if (!this.running) return;
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
    // Already answered "no live service" less than a minute ago: wait.
    if (this.state.enabled === false) {
      this.schedule();
      return;
    }
    if (this.deps.visible()) void this.tick();
  }

  private stop() {
    this.running = false;
    this.clear();
    this.controller?.abort();
  }

  private schedule() {
    this.clear();
    if (!this.running || !this.deps.visible()) return;
    this.timer = this.deps.setTimer(
      () => void this.tick(),
      this.state.enabled === false
        ? PULSE_OFF_MS
        : pulseDelay(this.state.failures),
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
        embedded: status.enabled === true ? status.embedded === true : null,
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
