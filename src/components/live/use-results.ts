"use client";
import { useCallback, useEffect, useState } from "react";
import { readResult } from "./result-api";
import type { ResultIndex, ResultItem } from "./result-types";
// Clear previous content synchronously by request identity; failed or blocked refreshes never retain old figures.
export function useResults(path: string | null, tick?: number | null) {
  const [attempt, setAttempt] = useState(0);
  const requestKey = JSON.stringify([path, attempt, tick ?? null]);
  const [state, setState] = useState<{
    key: string;
    data: ResultItem | ResultIndex | null;
    error: string;
  }>({ key: "", data: null, error: "" });
  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    setState({ key: requestKey, data: null, error: "" });
    void readResult(path, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted)
          setState({ key: requestKey, data, error: "" });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setState({
            key: requestKey,
            data: null,
            error: error instanceof Error ? error.message : String(error),
          });
      });
    return () => controller.abort();
  }, [path, requestKey]);
  const refresh = useCallback(() => setAttempt((n) => n + 1), []);
  return {
    data: state.key === requestKey ? state.data : null,
    error: state.key === requestKey ? state.error : "",
    refresh,
  };
}
