// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";

import React, {
  createContext,
  useContext,
  useState,
  useCallback,
  useMemo,
  useEffect,
} from "react";

const STORAGE_KEY = "levi-flags:";

function saveToStorage(s: Set<number>, repoId: string) {
  try {
    writeBrowserStorage("local", STORAGE_KEY + repoId, JSON.stringify([...s]));
  } catch {
    /* ignore */
  }
}

type FlaggedEpisodesContextType = {
  flagged: Set<number>;
  count: number;
  has: (id: number) => boolean;
  toggle: (id: number) => void;
  addMany: (ids: number[]) => void;
  clear: () => void;
};

const FlaggedEpisodesContext = createContext<
  FlaggedEpisodesContextType | undefined
>(undefined);

export function useFlaggedEpisodes() {
  const ctx = useContext(FlaggedEpisodesContext);
  if (!ctx)
    throw new Error(
      "useFlaggedEpisodes must be used within FlaggedEpisodesProvider",
    );
  return ctx;
}

export const FlaggedEpisodesProvider: React.FC<{
  children: React.ReactNode;
  repoId: string;
}> = ({ children, repoId }) => {
  const [flagged, setFlagged] = useState<Set<number>>(new Set());
  const [hydrated, setHydrated] = useState(false);

  // Dataset-scoped local draft; explicit exports are also persisted on the backend.
  useEffect(() => {
    let cancelled = false;
    setHydrated(false);
    const hydrate = async () => {
      let ids: number[] = [];
      try {
        const raw = readBrowserStorage("local", STORAGE_KEY + repoId);
        if (raw) ids = JSON.parse(raw);
        else {
          const response = await fetch(
            `/api/levi/review?repo_id=${encodeURIComponent(repoId)}`,
          );
          if (response.ok) ids = (await response.json()).flagged || [];
        }
      } catch {
        /* local operation remains available when backend is offline */
      }
      if (!cancelled) {
        setFlagged(new Set(ids));
        setHydrated(true);
      }
    };
    void hydrate();
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  // Only persist after hydration so the initial empty set doesn't
  // overwrite stored flags when the component remounts.
  useEffect(() => {
    if (!hydrated) return;
    saveToStorage(flagged, repoId);
  }, [flagged, hydrated, repoId]);

  const toggle = useCallback((id: number) => {
    setFlagged((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const addMany = useCallback((ids: number[]) => {
    setFlagged((prev) => {
      const next = new Set(prev);
      for (const id of ids) next.add(id);
      return next;
    });
  }, []);

  const clear = useCallback(() => setFlagged(new Set()), []);

  const has = useCallback((id: number) => flagged.has(id), [flagged]);

  const value = useMemo(
    () => ({
      flagged,
      count: flagged.size,
      has,
      toggle,
      addMany,
      clear,
    }),
    [flagged, has, toggle, addMany, clear],
  );

  return (
    <T>
      {
        <FlaggedEpisodesContext.Provider value={value}>
          <T>{children}</T>
        </FlaggedEpisodesContext.Provider>
      }
    </T>
  );
};
