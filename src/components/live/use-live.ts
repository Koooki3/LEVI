"use client";
// Polling for the live page. Read-only GETs, no websocket.
//   * 2 s while an evaluation runs, 10 s otherwise;
//   * nothing while the tab is hidden, an immediate refresh when it returns;
//   * after a failure the wait doubles (up to 30 s);
//   * with no live service to show, once a minute;
//   * a dataset's detail is fetched only for the few cards that matter or are
//     open, and again only when its row in the status changed.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  DETAIL_REFRESH_MS,
  isEvaluating,
  nextDelay,
  rowSignature,
} from "./live-logic";
import { PULSE_OFF_MS } from "./live-pulse-store";
import type {
  DatasetDetail,
  DatasetRow,
  LiveSessionsResponse,
  LiveStatusResponse,
} from "./types";

async function get<T>(path: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(`/api/levi/live/${path}`, {
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

export interface LivePoll {
  status: LiveStatusResponse | null;
  sessions: LiveSessionsResponse | null;
  /** Both requests failed (the LEVI core is not answering). */
  error: string;
  failures: number;
  /** Epoch milliseconds of the last successful poll. */
  lastOk: number | null;
  /** Milliseconds to the next request, for display. */
  delay: number;
}

export function useLivePoll(): LivePoll {
  const [state, setState] = useState<LivePoll>({
    status: null,
    sessions: null,
    error: "",
    failures: 0,
    lastOk: null,
    delay: nextDelay(false, 0),
  });
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let failures = 0;
    let evaluating = false;
    let lastStatus: LiveStatusResponse | null = null;
    let lastSessions: LiveSessionsResponse | null = null;
    const visible = () => document.visibilityState !== "hidden";
    const schedule = () => {
      if (stopped || !visible()) return;
      // No live service to show (the explanation page): look again once a
      // minute, so one started later appears without a reload.
      const wait =
        lastStatus?.enabled === false
          ? PULSE_OFF_MS
          : nextDelay(evaluating, failures);
      timer = setTimeout(() => void tick(), wait);
    };
    async function tick() {
      if (stopped || !visible()) return;
      controller = new AbortController();
      const signal = controller.signal;
      const [a, b] = await Promise.allSettled([
        get<LiveStatusResponse>("status", signal),
        get<LiveSessionsResponse>("sessions", signal),
      ]);
      if (stopped || signal.aborted) return;
      if (a.status === "rejected" && b.status === "rejected") {
        failures += 1;
        const reason = a.reason instanceof Error ? a.reason.message : "";
        setState((s) => ({
          ...s,
          error: reason || "Request failed",
          failures,
          delay: nextDelay(evaluating, failures),
        }));
      } else {
        failures = 0;
        if (a.status === "fulfilled") lastStatus = a.value;
        if (b.status === "fulfilled") lastSessions = b.value;
        evaluating = isEvaluating(
          lastSessions?.sessions ?? lastStatus?.service?.sessions ?? [],
          lastStatus?.service,
        );
        const snapshot = { status: lastStatus, sessions: lastSessions };
        setState((s) => ({
          ...s,
          ...snapshot,
          error: "",
          failures: 0,
          lastOk: Date.now(),
          delay: nextDelay(evaluating, 0),
        }));
      }
      schedule();
    }
    const onVisibility = () => {
      clearTimeout(timer);
      if (visible()) void tick();
      else controller?.abort();
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
  return state;
}

export interface DetailEntry {
  data: DatasetDetail | null;
  error: string;
  /** Epoch milliseconds of the request. */
  at: number;
  signature: string;
}

/** Details of the datasets in `wanted` (card order), refreshed when their
 * row changes and, for an open card that is being annotated, every 10 s. */
export function useDatasetDetails(
  wanted: string[],
  rows: Record<string, DatasetRow>,
  open: Set<string>,
  enabled: boolean,
): {
  details: Record<string, DetailEntry>;
  /** Fetch one dataset's detail again now (after a removal or a restore). */
  refresh: (name: string) => void;
} {
  const [details, setDetails] = useState<Record<string, DetailEntry>>({});
  const inflight = useRef(new Set<string>());
  const known = useRef<Record<string, DetailEntry>>({});
  const [beat, setBeat] = useState(0);

  const fetchOne = useCallback(async (name: string, signature: string) => {
    inflight.current.add(name);
    const controller = new AbortController();
    try {
      const data = await get<DatasetDetail>(
        `datasets/${encodeURIComponent(name)}`,
        controller.signal,
      );
      known.current[name] = { data, error: "", at: Date.now(), signature };
    } catch (e) {
      known.current[name] = {
        data: known.current[name]?.data ?? null,
        error: e instanceof Error ? e.message : String(e),
        at: Date.now(),
        signature,
      };
    } finally {
      inflight.current.delete(name);
    }
    setDetails({ ...known.current });
  }, []);

  const key = wanted
    .map((n) => `${n}:${rowSignature(rows[n])}:${open.has(n) ? 1 : 0}`)
    .join(";");
  useEffect(() => {
    if (!enabled) return;
    let started = 0;
    for (const name of wanted) {
      if (started >= 3 || inflight.current.has(name) || !rows[name]) continue;
      const signature = rowSignature(rows[name]);
      const entry = known.current[name];
      const stale =
        !entry ||
        entry.signature !== signature ||
        (open.has(name) &&
          rows[name].state === "annotating" &&
          Date.now() - entry.at > DETAIL_REFRESH_MS);
      if (stale) {
        started += 1;
        void fetchOne(name, signature);
      }
    }
    // `key` stands for wanted, rows and open.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, beat, fetchOne]);

  // Slow beat so an open, annotating card keeps moving; nothing while hidden.
  const anyBusy = wanted.some(
    (n) => open.has(n) && rows[n]?.state === "annotating",
  );
  useEffect(() => {
    if (!enabled || !anyBusy) return;
    const timer = setInterval(() => {
      if (document.visibilityState !== "hidden") setBeat((b) => b + 1);
    }, DETAIL_REFRESH_MS);
    return () => clearInterval(timer);
  }, [enabled, anyBusy]);
  const refresh = useCallback(
    (name: string) => {
      if (!inflight.current.has(name))
        void fetchOne(name, known.current[name]?.signature ?? "");
    },
    [fetchOne],
  );
  return { details, refresh };
}
