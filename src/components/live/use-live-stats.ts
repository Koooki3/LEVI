"use client";
// The statistics panel's data: `GET /api/levi/live/stats`, read-only. It does
// not poll on its own: it asks again when the page's poll has moved (`tick`),
// at most every 5 s while an evaluation runs and every 30 s otherwise, and not
// at all while the tab is hidden (the page's poll stops then). The request is
// a function of the whole scope (`statsPath`), so anything that changes it,
// the removed-episodes switch included, asks again at once.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

export interface RefreshableLiveStats extends LiveStats {
  refresh: () => void;
}

export function useLiveStats(
  scope: StatsScope,
  limit: number,
  tick: number | null,
  evaluating: boolean,
  enabled: boolean,
): RefreshableLiveStats {
  const [state, setState] = useState<LiveStats & { dataPath: string | null }>({
    data: null,
    dataPath: null,
    error: "",
    loading: false,
  });
  const last = useRef<number | null>(null);
  const generation = useRef(0);
  const loader = useMemo(() => createStatsLoader(fetch), []);
  // The one thing the request depends on, whole.
  const path = statsPath(scope, limit);
  const wanted = useRef({ scope, limit });
  wanted.current = { scope, limit };
  const current = useRef({ path, enabled });
  current.current = { path, enabled };

  const run = useRef(async () => {});
  run.current = async () => {
    last.current = Date.now();
    const mine = ++generation.current;
    const { scope: now, limit: size } = wanted.current;
    const requestedPath = statsPath(now, size);
    const isCurrent = () =>
      generation.current === mine &&
      current.current.enabled &&
      current.current.path === requestedPath;
    setState({ data: null, dataPath: null, error: "", loading: true });
    try {
      const data = await loader.load(now, size);
      if (data && isCurrent())
        setState({
          data,
          dataPath: statsPath(now, size),
          error: "",
          loading: false,
        });
    } catch (e) {
      if (
        !isCurrent() ||
        (e instanceof DOMException && e.name === "AbortError")
      )
        return;
      setState({
        data: null,
        dataPath: null,
        error: e instanceof Error ? e.message : String(e),
        loading: false,
      });
    }
  };

  // A changed request loads at once.
  useEffect(() => {
    if (enabled) void run.current();
    else {
      ++generation.current;
      loader.stop();
      last.current = null;
      setState({ data: null, dataPath: null, error: "", loading: false });
    }
  }, [path, enabled, loader]);

  // The page's poll moved: refresh when it is due.
  useEffect(() => {
    if (!enabled || tick == null) return;
    if (document.visibilityState === "hidden") return;
    if (dueForRefresh(Date.now(), last.current, evaluating)) void run.current();
  }, [tick, enabled, evaluating]);

  useEffect(
    () => () => {
      ++generation.current;
      loader.stop();
    },
    [loader],
  );
  // A manual refresh is independent of the status heartbeat and its throttle.
  const refresh = useCallback(() => {
    if (enabled && document.visibilityState !== "hidden") void run.current();
  }, [enabled]);
  return {
    ...state,
    data: enabled && state.dataPath === path ? state.data : null,
    refresh,
  };
}
