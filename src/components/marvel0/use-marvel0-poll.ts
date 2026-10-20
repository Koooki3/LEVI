"use client";
// Polling for the marvel0.
// Not usePolled (campaign-poll): it keeps the last good value on failure.
// Two read-only GETs of LEVI's own routes
// (the same-origin proxy adds the person's token; nothing here reads one),
// no websocket and no request to any robot-side port:
//   * every 3 s while the tab is visible, nothing while it is hidden, and an
//     immediate refresh when it returns;
//   * after a round in which both requests failed the wait doubles (up to
//     30 s);
//   * a request that has not answered within MARVEL0_REQUEST_MS is given up (and
//     told to stop), so one that never settles cannot hold the round, freeze
//     the polling or keep the last answer on screen;
//   * an answer older than MARVEL0_FRESH_MS (counting the age its sample already
//     had when it came) is dropped when its request fails or the tab
//     returns, so the page never shows an old sample as current.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  MARVEL0_REQUEST_MS,
  isFresh,
  marvel0Delay,
  sampleAgeS,
} from "./marvel0-logic";

const SETUP_STATUS = "/api/levi/setup/status";
const MARVEL0_STATUS = "/api/levi/marvel0/status";

async function fetchJson(path: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(path, { cache: "no-store", signal });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

/** One GET with a deadline. `signal` is the round's: when it aborts, the
 * request is dropped at once. A race, not only an abort of the request, so
 * one that ignores its signal (or a body that never ends) cannot hold the
 * round either. Not AbortSignal.timeout/any: older browsers lack them, and a
 * timer of our own is one the tests can fire. */
async function get(path: string, signal: AbortSignal): Promise<unknown> {
  const request = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let dropped = () => {};
  const cut = new Promise<never>((_, reject) => {
    // Rejects first, then tells the request to stop: the reason shown is ours,
    // not the abort error the request answers with.
    const giveUp = (why: string) => {
      reject(new Error(why));
      request.abort();
    };
    dropped = () => giveUp("aborted");
    timer = setTimeout(
      () => giveUp(`no answer within ${MARVEL0_REQUEST_MS / 1000} s`),
      MARVEL0_REQUEST_MS,
    );
  });
  if (signal.aborted) dropped();
  else signal.addEventListener("abort", dropped, { once: true });
  try {
    return await Promise.race([fetchJson(path, request.signal), cut]);
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", dropped);
  }
}

export interface Marvel0Poll {
  /** The first round of requests has finished (either way). */
  loaded: boolean;
  setup: unknown;
  marvel0: unknown;
  /** The last failure of each request, "" when the last one worked. */
  setupError: string;
  marvel0Error: string;
  /** Rounds in a row in which both requests failed. */
  failures: number;
  /** Epoch milliseconds of the last answer of either request. */
  lastOk: number | null;
  /** Milliseconds to the next round, for display. */
  delay: number;
  refresh: () => void;
}

interface Slot {
  value: unknown;
  at: number | null;
}

export function useMarvel0Poll(): Marvel0Poll {
  // Asks again at once; set by the effect below, which owns the timer.
  const again = useRef<() => void>(() => {});
  const refresh = useCallback(() => again.current(), []);
  const [state, setState] = useState<Omit<Marvel0Poll, "refresh">>({
    loaded: false,
    setup: null,
    marvel0: null,
    setupError: "",
    marvel0Error: "",
    failures: 0,
    lastOk: null,
    delay: marvel0Delay(0),
  });
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let failures = 0;
    let lastOk: number | null = null;
    const setup: Slot = { value: null, at: null };
    const marvel0: Slot = { value: null, at: null };
    const visible = () => document.visibilityState !== "hidden";
    const schedule = () => {
      if (stopped || !visible()) return;
      timer = setTimeout(() => void tick(), marvel0Delay(failures));
    };
    // Drops an answer that is too old to be shown as current.
    const expire = (slot: Slot, now: number) => {
      if (!isFresh(slot.at, now, sampleAgeS(slot.value))) slot.value = null;
    };
    const take = (
      slot: Slot,
      result: PromiseSettledResult<unknown>,
      now: number,
    ): string => {
      if (result.status === "fulfilled") {
        slot.value = result.value;
        slot.at = now;
        return "";
      }
      expire(slot, now);
      return result.reason instanceof Error
        ? result.reason.message
        : String(result.reason);
    };
    async function tick() {
      if (stopped || !visible()) return;
      controller = new AbortController();
      const signal = controller.signal;
      const [a, b] = await Promise.allSettled([
        get(SETUP_STATUS, signal),
        get(MARVEL0_STATUS, signal),
      ]);
      if (stopped || signal.aborted) return;
      const now = Date.now();
      const setupError = take(setup, a, now);
      const marvel0Error = take(marvel0, b, now);
      failures =
        a.status === "rejected" && b.status === "rejected" ? failures + 1 : 0;
      if (a.status === "fulfilled" || b.status === "fulfilled") lastOk = now;
      setState({
        loaded: true,
        setup: setup.value,
        marvel0: marvel0.value,
        setupError,
        marvel0Error,
        failures,
        lastOk,
        delay: marvel0Delay(failures),
      });
      schedule();
    }
    const onVisibility = () => {
      clearTimeout(timer);
      if (visible()) {
        // Answers from before the tab was hidden are not current.
        const now = Date.now();
        expire(setup, now);
        expire(marvel0, now);
        setState((s) => ({ ...s, setup: setup.value, marvel0: marvel0.value }));
        void tick();
      } else controller?.abort();
    };
    again.current = () => {
      clearTimeout(timer);
      controller?.abort();
      void tick();
    };
    document.addEventListener("visibilitychange", onVisibility);
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);
  return { ...state, refresh };
}
