"use client";
// The statistics panel's data: `GET /api/levi/live/stats`, read-only. It does
// not poll on its own: it asks again when the page's poll has moved (`tick`),
// at most every 5 s while an evaluation runs and every 30 s otherwise, and not
// at all while the tab is hidden (the page's poll stops then). The request is
// a function of the whole scope (`statsPath`), so anything that changes it,
// the removed-episodes switch included, asks again at once.
import { useEffect, useMemo, useRef, useState } from "react";
import {
  createStatsLoader,
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
  const loader = useMemo(() => createStatsLoader(fetch), []);
  // The one thing the request depends on, whole.
  const path = statsPath(scope, limit);
  const wanted = useRef({ scope, limit });
  wanted.current = { scope, limit };

  const run = useRef(async () => {});
  run.current = async () => {
    last.current = Date.now();
    setState((s) => ({ ...s, loading: true }));
    try {
      const { scope: now, limit: size } = wanted.current;
      const data = await loader.load(now, size);
      if (data) setState({ data, error: "", loading: false });
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setState((s) => ({
        data: s.data,
        error: e instanceof Error ? e.message : String(e),
        loading: false,
      }));
    }
  };

  // A changed request loads at once.
  useEffect(() => {
    if (enabled) void run.current();
  }, [path, enabled]);

  // The page's poll moved: refresh when it is due.
  useEffect(() => {
    if (!enabled || tick == null) return;
    if (document.visibilityState === "hidden") return;
    if (dueForRefresh(Date.now(), last.current, evaluating)) void run.current();
  }, [tick, enabled, evaluating]);

  useEffect(() => () => loader.stop(), [loader]);
  return state;
}
