"use client";
import { useEffect, useState } from "react";

export type Facets = {
  episodes: number[];
  cameras: string[];
  tasks: string[];
  loading: boolean;
  error: string;
};

const EMPTY: Facets = {
  episodes: [],
  cameras: [],
  tasks: [],
  loading: false,
  error: "",
};

/** What a dataset actually contains, so the plan form can offer it.
 *
 * Read straight from the dataset's own metadata through the same-origin file
 * service: episode count and camera keys from `meta/info.json`, task strings
 * from `meta/tasks.jsonl` when the dataset has more than one.
 */
export function useDatasetFacets(repoId: string): Facets {
  const [facets, setFacets] = useState<Facets>(EMPTY);

  useEffect(() => {
    const name = repoId.startsWith("local/") ? repoId.slice(6) : "";
    if (!name) {
      setFacets(EMPTY);
      return;
    }
    let cancelled = false;
    setFacets({ ...EMPTY, loading: true });
    const base = `/api/levi/files/${encodeURIComponent(name)}`;

    async function load() {
      const response = await fetch(`${base}/meta/info.json`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`meta/info.json: ${response.status}`);
      const info = (await response.json()) as {
        total_episodes?: number;
        features?: Record<string, { dtype?: string }>;
      };
      const cameras = Object.entries(info.features ?? {})
        .filter(([, value]) => value?.dtype === "video")
        .map(([key]) => key);
      const episodes = Array.from(
        { length: info.total_episodes ?? 0 },
        (_, index) => index,
      );
      let tasks: string[] = [];
      try {
        const list = await fetch(`${base}/meta/tasks.jsonl`, {
          cache: "no-store",
        });
        if (list.ok) {
          tasks = [
            ...new Set(
              (await list.text())
                .split("\n")
                .filter(Boolean)
                .map(
                  (line) => (JSON.parse(line) as { task?: string }).task || "",
                )
                .filter(Boolean),
            ),
          ];
        }
      } catch {
        // A dataset without a task list is normal; the field simply hides.
      }
      if (!cancelled)
        setFacets({ episodes, cameras, tasks, loading: false, error: "" });
    }

    void load().catch((error: unknown) => {
      if (!cancelled) {
        setFacets({
          ...EMPTY,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  return facets;
}
