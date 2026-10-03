"use client";
// The statistics panel's data: `GET /api/levi/live/stats`, read-only. It does
// not poll on its own: it asks again when the page's poll has moved (`tick`),
// at most every 5 s while an evaluation runs and every 30 s otherwise, and
// not at all while the tab is hidden (the page's poll stops then).
import { useCallback, useEffect, useRef, useState } from "react";
import {
  dueForRefresh,
  statsPath,
  type StatsResponse,
  type StatsScope,
} from "./stats-logic";

export interface LiveStats {
  data: StatsResponse | null;
  error: string;
  loading: boolean;
}

export function useLiveStats(
  scope: StatsScope,
  limit: number,
  tick: number | null,
  evaluating: boolean,
  enabled: boolean,
): LiveStats {
  const [state, setState] = useState<LiveStats>({
    data: null,
    error: "",
    loading: false,
  });
  const last = useRef<number | null>(null);
  const inflight = useRef<AbortController | null>(null);
  const key = `${scope.dataset}|${scope.session}|${limit}`;
  const keyRef = useRef(key);

  const load = useCallback(async () => {
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;
    last.current = Date.now();
    setState((s) => ({ ...s, loading: true }));
    try {
      const response = await fetch(
        `/api/levi/live/${statsPath(scope, limit)}`,
        {
          cache: "no-store",
          signal: controller.signal,
        },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data: StatsResponse = await response.json();
      if (controller.signal.aborted) return;
      setState({ data, error: "", loading: false });
    } catch (e) {
      if (controller.signal.aborted) return;
      setState((s) => ({
        data: s.data,
        error: e instanceof Error ? e.message : String(e),
        loading: false,
      }));
    }
    // `key` stands for scope and limit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // A changed scope or page size loads at once.
  useEffect(() => {
    if (!enabled) return;
    keyRef.current = key;
    void load();
  }, [key, enabled, load]);

  // The page's poll moved: refresh when it is due.
  useEffect(() => {
    if (!enabled || tick == null) return;
    if (document.visibilityState === "hidden") return;
    if (dueForRefresh(Date.now(), last.current, evaluating)) void load();
  }, [tick, enabled, evaluating, load]);

  useEffect(() => () => inflight.current?.abort(), []);
  return state;
}
