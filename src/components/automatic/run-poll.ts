"use client";
// Polling for the run window and the run list. Read-only GETs: 2 s while a run
// goes on, far less in a hidden tab (and an immediate refresh when it comes
// back), doubling after a failure. Nothing is polled once the page is closed.
import { useCallback, useEffect, useRef, useState } from "react";
import { AutomaticApiError, automaticApi } from "./api";
import { nextPollDelay } from "./run-logic";
import type { MetricsReport, RunEvent, RunSnapshot, RunSummary } from "./types";

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
