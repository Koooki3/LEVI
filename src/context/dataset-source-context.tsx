"use client";
/**
 * What the dataset open in the viewer is (levi/describe.py via the catalog),
 * and whether it changed on disk since the page loaded (levi/sync.py): lets
 * features adapt to raw captures, and lets the viewer offer a reload when
 * episodes were added, removed or rebuilt while it was open. Hub datasets
 * have no entry and get the full feature set.
 */
import React, { createContext, useContext, useEffect, useState } from "react";
import type { CatalogEntry, DatasetFormat } from "@/types/dataset-format.types";

/** How often an open viewer checks its dataset for changes on disk. */
export const DATASET_POLL_MS = 5000;

// The revision each dataset had when this page (JS context) first loaded
// it. Module-level on purpose: moving between episodes remounts the viewer
// but reuses cached dataset metadata, so the baseline must survive that and
// only reset on a real reload.
const baselines = new Map<
  string,
  { revision: string | null; episodes: number | null }
>();

interface DatasetSource {
  entry: CatalogEntry | null;
  format: DatasetFormat | null;
  isRaw: boolean;
  /** The dataset's metadata changed on disk since this page loaded. */
  changed: boolean;
  /** Episode count when the page loaded, for the update notice. */
  loadedEpisodes: number | null;
  /** The dataset was removed from the workspace. */
  removed: boolean;
}

const DatasetSourceContext = createContext<DatasetSource>({
  entry: null,
  format: null,
  isRaw: false,
  changed: false,
  loadedEpisodes: null,
  removed: false,
});

export function useDatasetSource(): DatasetSource {
  return useContext(DatasetSourceContext);
}

export function DatasetSourceProvider({
  org,
  dataset,
  children,
}: {
  org: string;
  dataset: string;
  children: React.ReactNode;
}) {
  const [entry, setEntry] = useState<CatalogEntry | null>(null);
  const [removed, setRemoved] = useState(false);
  const [changed, setChanged] = useState(false);
  const [loadedEpisodes, setLoadedEpisodes] = useState<number | null>(null);

  useEffect(() => {
    if (org !== "local") {
      setEntry(null);
      return;
    }
    setChanged(false);
    setRemoved(false);
    let cancelled = false;
    const poll = async () => {
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        const response = await fetch(
          `/api/levi/catalog/${encodeURIComponent(dataset)}`,
          { cache: "no-store" },
        );
        if (cancelled) return;
        if (response.status === 404) {
          setRemoved(true);
          return;
        }
        if (!response.ok) return;
        const next = (await response.json()) as CatalogEntry;
        if (cancelled) return;
        setRemoved(false);
        setEntry(next);
        const revision = next.revision ?? null;
        let baseline = baselines.get(dataset);
        if (!baseline) {
          baseline = { revision, episodes: next.format?.episodes ?? null };
          baselines.set(dataset, baseline);
        }
        setLoadedEpisodes(baseline.episodes);
        if (revision && revision !== baseline.revision) setChanged(true);
      } catch {
        /* offline backend: keep the last known state */
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), DATASET_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [org, dataset]);

  const format = entry?.format ?? null;
  return (
    <DatasetSourceContext.Provider
      value={{
        entry,
        format,
        isRaw: format?.kind === "raw",
        changed,
        loadedEpisodes,
        removed,
      }}
    >
      {children}
    </DatasetSourceContext.Provider>
  );
}
