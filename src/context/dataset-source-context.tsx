"use client";
/**
 * What the dataset open in the viewer is (levi/describe.py via the catalog):
 * lets features adapt to raw captures, which are browsed through a generated
 * view and cannot be exported directly. Hub datasets have no entry and get
 * the full feature set.
 */
import React, { createContext, useContext, useEffect, useState } from "react";
import type { CatalogEntry, DatasetFormat } from "@/types/dataset-format.types";

interface DatasetSource {
  entry: CatalogEntry | null;
  format: DatasetFormat | null;
  isRaw: boolean;
}

const DatasetSourceContext = createContext<DatasetSource>({
  entry: null,
  format: null,
  isRaw: false,
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
  useEffect(() => {
    if (org !== "local") {
      setEntry(null);
      return;
    }
    let cancelled = false;
    fetch("/api/levi/catalog", { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((catalog: { local?: CatalogEntry[] } | null) => {
        if (cancelled) return;
        setEntry(
          catalog?.local?.find((item) => item.id === `local/${dataset}`) ??
            null,
        );
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [org, dataset]);
  const format = entry?.format ?? null;
  return (
    <DatasetSourceContext.Provider
      value={{ entry, format, isRaw: format?.kind === "raw" }}
    >
      {children}
    </DatasetSourceContext.Provider>
  );
}
