"use client";
// Polling for the run window and the run list. Read-only GETs: 2 s while a run
// goes on, far less in a hidden tab (and an immediate refresh when it comes
// back), doubling after a failure. Nothing is polled once the page is closed.
import { useCallback, useEffect, useRef, useState } from "react";
import { AutomaticApiError, automaticApi } from "./api";
import { nextPollDelay } from "./run-logic";
import type {
  MetricsReport,
  RolloutsResponse,
  RunEvent,
  RunSnapshot,
  RunSummary,
  SubtasksResponse,
} from "./types";

export interface Polled<T> {
  data: T | null;
  /** The last error text, "" when the last request worked. */
  error: string;
  /** HTTP status of the last failed request (0: no answer). */
  status: number;
  failures: number;
  lastOk: number | null;
  refresh: () => void;
}

function usePolled<T>(
  load: (signal: AbortSignal) => Promise<T>,
  stateOf: (value: T | null) => string | undefined,
  key: string,
): Polled<T> {
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [failures, setFailures] = useState(0);
  const [status, setStatus] = useState(0);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const stateRef = useRef(stateOf);
  stateRef.current = stateOf;
  const refresh = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let failed = 0;
    let latest: T | null = null;
    const hidden = () => document.visibilityState === "hidden";
    const schedule = () => {
      if (stopped) return;
      timer = setTimeout(
        () => void tick(),
        nextPollDelay({
          hidden: hidden(),
          state: stateRef.current(latest),
          failures: failed,
        }),
      );
    };
    async function tick() {
      if (stopped) return;
      controller = new AbortController();
      try {
        const value = await loadRef.current(controller.signal);
        if (stopped) return;
        latest = value;
        failed = 0;
        setData(value);
        setError("");
        setStatus(0);
        setFailures(0);
        setLastOk(Date.now());
      } catch (caught) {
        if (stopped || (caught as Error)?.name === "AbortError") return;
        failed += 1;
        setError(caught instanceof Error ? caught.message : String(caught));
        setStatus(caught instanceof AutomaticApiError ? caught.status : 0);
        setFailures(failed);
      }
      schedule();
    }
    const onVisible = () => {
      if (stopped || hidden()) return;
      clearTimeout(timer);
      controller?.abort();
      void tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [key, revision]);

  return { data, error, status, failures, lastOk, refresh };
}

export function useRunList(): Polled<RunSummary[]> {
  return usePolled<RunSummary[]>(
    async (signal) => (await automaticApi.runs(signal)).runs,
    () => undefined,
    "runs",
  );
}

export function useRunSnapshot(runId: string): Polled<RunSnapshot> {
  return usePolled<RunSnapshot>(
    (signal) => automaticApi.snapshot(runId, signal),
    (value) => value?.state,
    `run:${runId}`,
  );
}

const MAX_EVENTS = 400;

/** The event tail, fetched again (from the last `seq`) when the run's `seq` moves. */
export function useRunEvents(runId: string, seq: number | undefined) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const cursor = useRef(0);
  useEffect(() => {
    cursor.current = 0;
    setEvents([]);
  }, [runId]);
  useEffect(() => {
    if (seq === undefined) return;
    let stopped = false;
    const controller = new AbortController();
    (async () => {
      try {
        for (let page = 0; page < 20 && !stopped; page++) {
          const got = await automaticApi.events(
            runId,
            cursor.current,
            controller.signal,
          );
          if (stopped) return;
          if (got.events.length)
            setEvents((old) => [...old, ...got.events].slice(-MAX_EVENTS));
          cursor.current = Math.max(cursor.current, got.next);
          if (got.events.length < 200) break;
        }
      } catch {
        /* the next change of `seq` tries again */
      }
    })();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [runId, seq]);
  return events;
}

/** A subtask-loop run's notes and plan files, fetched again when the run's
 * `seq` moves. `enabled` false (any other reset mode) fetches nothing. */
export function useRunSubtasks(
  runId: string,
  seq: number | undefined,
  enabled: boolean,
) {
  const [data, setData] = useState<SubtasksResponse | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (seq === undefined || !enabled) return;
    let stopped = false;
    const controller = new AbortController();
    automaticApi
      .subtasks(runId, controller.signal)
      .then((value) => {
        if (stopped) return;
        setData(value);
        setError("");
      })
      .catch((caught) => {
        if (stopped || caught?.name === "AbortError") return;
        setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [runId, seq, enabled]);
  return { data, error };
}

/** The rollout sets, fetched once `enabled` (the view is open) and again
 * when the run's `seq` moved and at least `ROLLOUTS_REFRESH_MS` passed: the
 * manifest is rebuilt from every demo's metadata, so it is not re-read on
 * every journal line. */
export const ROLLOUTS_REFRESH_MS = 15_000;
export function useRunRollouts(
  runId: string,
  seq: number | undefined,
  enabled: boolean,
) {
  const [data, setData] = useState<RolloutsResponse | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const last = useRef<{ at: number; seq: number | undefined } | null>(null);
  // A seq change inside the refresh window skips a fetch, so it must not
  // cancel the one in flight: only unmounting or another run does.
  const request = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      request.current?.abort();
      request.current = null;
      last.current = null;
    },
    [runId],
  );
  useEffect(() => {
    if (!enabled) return;
    const now = Date.now();
    const before = last.current;
    if (before && (before.seq === seq || now - before.at < ROLLOUTS_REFRESH_MS))
      return;
    last.current = { at: now, seq };
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    automaticApi
      .rollouts(runId, controller.signal)
      .then((value) => {
        if (request.current !== controller) return;
        setData(value);
        setError("");
      })
      .catch((caught) => {
        if (request.current !== controller || caught?.name === "AbortError")
          return;
        // Let the next open or seq change try again.
        last.current = null;
        setError(caught instanceof Error ? caught.message : String(caught));
      })
      .finally(() => {
        if (request.current !== controller) return;
        request.current = null;
        setLoading(false);
      });
  }, [runId, seq, enabled]);
  return { data, error, loading };
}

/** The metrics report, fetched again when the run's `seq` moves. */
export function useRunMetrics(runId: string, seq: number | undefined) {
  const [report, setReport] = useState<MetricsReport | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (seq === undefined) return;
    let stopped = false;
    const controller = new AbortController();
    automaticApi
      .metrics(runId, controller.signal)
      .then((value) => {
        if (stopped) return;
        setReport(value);
        setError("");
      })
      .catch((caught) => {
        if (stopped || caught?.name === "AbortError") return;
        setError(caught instanceof Error ? caught.message : String(caught));
      });
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [runId, seq]);
  return { report, error };
}
