"use client";

import { useEffect, useState } from "react";

/** Local metadata is authoritative: deleted IDs must never come back from
 * range(total_episodes), cached task tables, or a session's neighbouring run. */
export function useLocalEpisodes(
  org: string,
  dataset: string,
  session?: string | null,
  revision = 0,
) {
  const scope = JSON.stringify([org, dataset, session || null, revision]);
  const [state, setState] = useState<{
    scope: string;
    indices: number[] | null;
    error: string;
    loading: boolean;
  }>({ scope, indices: null, error: "", loading: org === "local" });
  useEffect(() => {
    if (org !== "local") {
      setState({ scope, indices: null, error: "", loading: false });
      return;
    }
    const controller = new AbortController();
    setState({ scope, indices: null, error: "", loading: true });
    const query = session ? `?live_session=${encodeURIComponent(session)}` : "";
    void fetch(
      `/api/levi/datasets/${encodeURIComponent(dataset)}/episodes${query}`,
      { cache: "no-store", signal: controller.signal },
    )
      .then(async (response) => {
        const value = await response.json();
        if (!response.ok)
          throw new Error(value.detail || `HTTP ${response.status}`);
        if (
          !Array.isArray(value.indices) ||
          !value.indices.every(
            (n: unknown) =>
              typeof n === "number" && Number.isInteger(n) && n >= 0,
          )
        )
          throw new Error("Invalid episode metadata");
        if (!controller.signal.aborted)
          setState({
            scope,
            indices: value.indices,
            error: "",
            loading: false,
          });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setState({
            scope,
            indices: null,
            error: String(error.message || error),
            loading: false,
          });
      });
    return () => controller.abort();
  }, [org, dataset, session, scope]);
  return state.scope === scope
    ? state
    : { indices: null, error: "", loading: org === "local" };
}

export function episodeHref(episode: number, session?: string | null): string {
  return `./episode_${episode}${session ? `?live_session=${encodeURIComponent(session)}` : ""}`;
}
