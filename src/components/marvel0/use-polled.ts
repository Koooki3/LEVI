"use client";
// One polled document of the marvel0, for the arm stack (every 2 s) and the
// hosted client (every 1 s): the same rules as the policy server's poll
// (use-marvel0-actions.ts), in one place.
//
//   * nothing is asked while the tab is hidden, and one read is made at once
//     when it returns (an answer from before the tab was hidden is dropped
//     first, so the page never shows an old state as current);
//   * a failed read doubles the wait, up to 30 s, and drops the last answer
//     when it is older than `freshMs`;
//   * `refresh()` reads now and resolves when that read has finished, so a
//     card can show "working..." from a click until the page has seen the
//     result;
//   * `lastOk` (epoch ms) is what the "last update" line counts from.
//
// Only LEVI's own routes are asked, through the same-origin proxy; nothing
// here reads a token or connects to a robot-side port.
import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiFailure, ApiResult } from "./deploy-api";

export type PollProblem = ApiFailure | { format: true };

export interface Polled<T> {
  /** The first read has finished (either way). */
  loaded: boolean;
  /** The last answer, dropped when it is too old to be current. */
  value: T | null;
  /** The last read worked: `value` is current. */
  current: boolean;
  problem: PollProblem | null;
  /** Epoch milliseconds of the last good read. */
  lastOk: number | null;
  /** Reads now; resolves when that read has finished. */
  refresh: () => Promise<void>;
}

export interface PolledOptions<T> {
  read: (signal: AbortSignal) => Promise<ApiResult>;
  /** The document in the format this page was written for, else null. */
  parse: (data: unknown) => T | null;
  pollMs: number;
  /** An answer older than this is not shown as current. */
  freshMs: number;
  /** False reads nothing (a card whose backend is off). */
  enabled?: boolean;
}

const MAX_WAIT_MS = 30_000;

/** The wait before the next read: the pace, doubling after each failure. */
export const pollDelay = (failures: number, pollMs: number): number =>
  failures <= 0
    ? pollMs
    : Math.min(MAX_WAIT_MS, pollMs * 2 ** Math.min(failures, 6));

export function usePolled<T>(options: PolledOptions<T>): Polled<T> {
  const { pollMs, freshMs, enabled = true } = options;
  const readRef = useRef(options.read);
  const parseRef = useRef(options.parse);
  readRef.current = options.read;
  parseRef.current = options.parse;
  const again = useRef<() => Promise<void>>(async () => {});
  const refresh = useCallback(() => again.current(), []);
  const [state, setState] = useState<Omit<Polled<T>, "refresh">>({
    loaded: false,
    value: null,
    current: false,
    problem: null,
    lastOk: null,
  });
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let failures = 0;
    let lastOk: number | null = null;
    let value: T | null = null;
    const visible = () => document.visibilityState !== "hidden";
    const schedule = () => {
      if (stopped || !visible()) return;
      timer = setTimeout(() => void tick(), pollDelay(failures, pollMs));
    };
    const expire = (now: number) => {
      if (lastOk === null || now - lastOk > freshMs) value = null;
    };
    async function tick(): Promise<void> {
      if (stopped || !visible()) return;
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      const result = await readRef.current(signal).catch(() => null);
      if (!result || stopped || signal.aborted) return; // a newer read, or left
      const now = Date.now();
      let parsed: T | null = null;
      if (result.ok) {
        try {
          parsed = parseRef.current(result.data);
        } catch {
          parsed = null;
        }
      }
      let problem: PollProblem | null = null;
      if (parsed !== null) {
        value = parsed;
        lastOk = now;
        failures = 0;
      } else {
        problem = result.ok ? { format: true } : result;
        failures += 1;
        expire(now);
      }
      setState({
        loaded: true,
        value,
        current: problem === null,
        problem,
        lastOk,
      });
      schedule();
    }
    const onVisibility = () => {
      clearTimeout(timer);
      if (visible()) {
        expire(Date.now());
        setState((s) => ({ ...s, value, current: false }));
        void tick();
      } else controller?.abort();
    };
    again.current = async () => {
      clearTimeout(timer);
      await tick();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [pollMs, freshMs, enabled]);
  return { ...state, refresh };
}
