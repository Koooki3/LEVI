// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

/**
 * Per-episode annotation state for the v3.1 language schema.
 *
 * - Atoms live in memory + sessionStorage so the user can browse without a
 *   backend (read/edit, but no parquet rewrite).
 * - When `NEXT_PUBLIC_ANNOTATE_BACKEND_URL` is set, the context syncs with
 *   the FastAPI service in `backend/`: GET on episode entry, POST on save,
 *   plus frame-timestamp fetches used to snap event-style atoms to exact
 *   source-frame timestamps (the writer in lerobot#3471 enforces exact match).
 *
 * - VQA drawings (active `pendingDraw`) live here too so the panel and the
 *   video overlay component share a single source of truth.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { LanguageAtom } from "../types/language.types";
import { snapToFrame } from "../types/language.types";
import {
  fetchEpisodeAtoms,
  saveEpisodeAtoms,
  deleteEpisodeAtoms,
  fetchFrameTimestamps,
  isAnnotateBackendEnabled,
} from "../utils/annotationsClient";

const STORAGE_PREFIX = "lerobot-annotations:v2:";

function storageKey(repoOrPath: string, episodeId: number): string {
  return `${STORAGE_PREFIX}${repoOrPath}::${episodeId}`;
}

export interface PendingBboxDraw {
  kind: "bbox";
  bbox: [number, number, number, number]; // 0..1, image-relative
  label: string;
  camera?: string;
}

export interface PendingPointDraw {
  kind: "keypoint";
  point: [number, number]; // 0..1, image-relative
  label: string;
  camera?: string;
}

export type PendingDraw = PendingBboxDraw | PendingPointDraw | null;
/**
 * `"auto"` — drag = bbox, single click = keypoint (the natural mode the
 * Annotations tab boots into). The other values force a single gesture
 * and exist for the legacy panel-driven flow.
 */
export type DrawMode = "off" | "auto" | "bbox" | "keypoint";

interface DatasetIdent {
  repoId?: string | null;
  localPath?: string | null;
  revision?: string | null;
}

interface AnnotationsContextType {
  episodeId: number | null;
  ident: DatasetIdent;
  atoms: LanguageAtom[];
  frameTimestamps: number[];
  /**
   * Index in `atoms` of the currently selected atom (the one the right-rail
   * editor is bound to). `null` means nothing is selected — the editor shows
   * an empty state. Selection survives content edits because we mutate atoms
   * in place at the same index; we clear it on delete or when atoms reset.
   */
  selectedIdx: number | null;
  selectAtom: (idx: number | null) => void;
  /**
   * Active <video> element for the camera the user is currently drawing on.
   * Registered by `VideoOverlayCanvas`. Used by the panel to read the
   * authoritative `currentTime` (the time-context's value is throttled and
   * can lag the real video by tens of ms — enough to land an annotation on
   * the wrong frame after a snap to the nearest frame timestamp).
   */
  activeVideoEl: HTMLVideoElement | null;
  setActiveVideoEl: (el: HTMLVideoElement | null) => void;
  pendingDraw: PendingDraw;
  // Selected camera for the drawing overlay (e.g. "observation.images.top").
  // Determines which video the next drawn bbox/point should be associated with.
  activeCamera: string | null;
  drawMode: DrawMode;
  drawLabel: string;
  backendEnabled: boolean;
  dirty: boolean;
  saving: boolean;

  setEpisode: (
    episodeId: number,
    ident: DatasetIdent,
    initialAtoms?: LanguageAtom[],
    initialFrameTimestamps?: number[],
  ) => void;
  setActiveCamera: (camera: string | null) => void;
  setDrawMode: (mode: DrawMode) => void;
  setDrawLabel: (label: string) => void;

  addAtom: (atom: LanguageAtom) => void;
  addAtoms: (atoms: LanguageAtom[]) => void;
  updateAtom: (index: number, updates: Partial<LanguageAtom>) => void;
  deleteAtom: (atom: LanguageAtom) => void;
  resetAtoms: () => void;
  /**
   * Ctrl+Z / Ctrl+Shift+Z (or Ctrl+Y) are already wired globally while this
   * provider is mounted — these are exposed mainly so a future undo/redo
   * button could call them directly. Scoped to the current episode: the
   * stacks reset on `setEpisode`. No-ops when there's nothing to undo/redo.
   */
  undo: () => void;
  redo: () => void;

  setPendingDraw: (draw: PendingDraw) => void;
  clearPendingDraw: () => void;

  save: () => Promise<{ ok: boolean; error?: string; path?: string | null }>;
  /**
   * Delete this episode's annotation file entirely and reset local atoms
   * to empty — distinct from `save()` with an empty array (see its doc).
   */
  deleteEpisodeFile: () => Promise<{ ok: boolean; error?: string }>;
  /**
   * Flush every OTHER episode's sessionStorage-cached edits for this dataset
   * to the backend. Call before exporting: `save()` only ever persists the
   * currently open episode, so edits made to other episodes earlier in the
   * session — and never explicitly "Save episode"'d — would otherwise be
   * silently absent from the export. No-op without a backend.
   */
  flushAllEpisodes: () => Promise<void>;
  // Snap an arbitrary timestamp to the nearest source frame (when known).
  snap: (ts: number) => number;
}

const AnnotationsContext = createContext<AnnotationsContextType | undefined>(
  undefined,
);

export function useAnnotations(): AnnotationsContextType {
  const ctx = useContext(AnnotationsContext);
  if (!ctx) {
    throw new Error("useAnnotations must be used within AnnotationsProvider");
  }
  return ctx;
}

function identKey(ident: DatasetIdent): string {
  return ident.localPath || ident.repoId || "unknown";
}

export const AnnotationsProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [episodeId, setEpisodeId] = useState<number | null>(null);
  const [ident, setIdent] = useState<DatasetIdent>({});
  const [atoms, setAtoms] = useState<LanguageAtom[]>([]);
  const [frameTimestamps, setFrameTimestamps] = useState<number[]>([]);
  const [pendingDraw, setPendingDrawState] = useState<PendingDraw>(null);
  const [activeCamera, setActiveCameraState] = useState<string | null>(null);
  const [drawMode, setDrawModeState] = useState<DrawMode>("off");
  const [drawLabel, setDrawLabelState] = useState<string>("");
  const [activeVideoEl, setActiveVideoElState] =
    useState<HTMLVideoElement | null>(null);
  const [selectedIdx, setSelectedIdxState] = useState<number | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const backendEnabled = isAnnotateBackendEnabled();

  // Track the last saved snapshot to detect dirtiness honestly.
  const savedSnapshotRef = useRef<string>("[]");

  const loadGeneration = useRef(0);

  // ---- Undo/redo -----------------------------------------------------
  // Scoped to the current episode: switching episodes clears both stacks
  // (see setEpisode below) rather than letting Ctrl+Z reach across episode
  // boundaries. Two-stack model: undo pops `history` and pushes the
  // superseded state onto `redo`; redo does the reverse. Refs, not state —
  // nothing here needs to trigger a render on its own; `atoms` changing is
  // what drives re-renders.
  const HISTORY_LIMIT = 50;
  const historyRef = useRef<LanguageAtom[][]>([]);
  const redoRef = useRef<LanguageAtom[][]>([]);
  // Rapid consecutive edits to the same thing (typing into a content
  // textarea commits on every keystroke via updateAtom) are grouped into
  // one undo step instead of one per character: the first edit in a burst
  // opens a pending group holding the state from *before* the burst; each
  // further edit within the group extends its timeout instead of pushing
  // its own entry; the group closes (and finally lands on the stack) after
  // a short pause.
  const pendingGroupRef = useRef<{
    baseline: LanguageAtom[];
    timer: ReturnType<typeof setTimeout>;
  } | null>(null);

  const flushPendingGroup = useCallback(() => {
    const pending = pendingGroupRef.current;
    if (!pending) return;
    clearTimeout(pending.timer);
    historyRef.current.push(pending.baseline);
    if (historyRef.current.length > HISTORY_LIMIT) historyRef.current.shift();
    pendingGroupRef.current = null;
  }, []);

  // Every mutator calls this with the atoms value as it stood immediately
  // before the change it's about to make (read inside its own setAtoms
  // updater, so it's always the true pre-change state even under rapid
  // calls). A new edit always invalidates redo, same as any other editor.
  const recordBeforeChange = useCallback(
    (before: LanguageAtom[]) => {
      redoRef.current = [];
      const pending = pendingGroupRef.current;
      if (pending) {
        clearTimeout(pending.timer);
        pending.timer = setTimeout(flushPendingGroup, 600);
      } else {
        pendingGroupRef.current = {
          baseline: before,
          timer: setTimeout(flushPendingGroup, 600),
        };
      }
    },
    [flushPendingGroup],
  );

  // Hydrate from sessionStorage when episode/ident changes; if the backend
  // is enabled, also fetch authoritative atoms + frame timestamps.
  const setEpisode = useCallback(
    (
      newEpisodeId: number,
      newIdent: DatasetIdent,
      initialAtoms?: LanguageAtom[],
      initialFrameTimestamps?: number[],
    ) => {
      const generation = ++loadGeneration.current;
      setEpisodeId(newEpisodeId);
      setIdent(newIdent);
      setPendingDrawState(null);
      setSelectedIdxState(null);
      // Undo history doesn't reach across episodes.
      if (pendingGroupRef.current) clearTimeout(pendingGroupRef.current.timer);
      pendingGroupRef.current = null;
      historyRef.current = [];
      redoRef.current = [];

      // Hydrate from session first (so user edits survive episode toggles).
      // If session is empty, fall back to initialAtoms (parquet-extracted).
      let initial: LanguageAtom[] = [];
      try {
        const raw = sessionStorage.getItem(
          storageKey(identKey(newIdent), newEpisodeId),
        );
        if (raw) initial = JSON.parse(raw) as LanguageAtom[];
      } catch {
        /* ignore */
      }
      if (initial.length === 0 && initialAtoms && initialAtoms.length > 0) {
        initial = initialAtoms;
      }
      setAtoms(initial);
      savedSnapshotRef.current = JSON.stringify(initial);
      setDirty(false);
      // Seed frame timestamps from the parquet (no backend dependency); the
      // backend will optionally overwrite this below.
      setFrameTimestamps(initialFrameTimestamps ?? []);

      // Fetch from backend if available.
      if (isAnnotateBackendEnabled()) {
        fetchEpisodeAtoms(newEpisodeId, newIdent)
          .then((remoteAtoms) => {
            // Prefer backend if it has anything; otherwise keep session-cached
            // edits the user made before the backend came online.
            if (generation !== loadGeneration.current) return;
            setAtoms((current) => {
              if (JSON.stringify(current) !== JSON.stringify(initial))
                return current;
              savedSnapshotRef.current = JSON.stringify(remoteAtoms);
              setDirty(false);
              return remoteAtoms;
            });
          })
          .catch(() => {
            /* backend offline — silent fallback to sessionStorage */
          });

        fetchFrameTimestamps(newEpisodeId, newIdent)
          .then((ts) => {
            if (generation === loadGeneration.current && ts.length)
              setFrameTimestamps(ts);
          })
          .catch(() => {
            /* retain timestamps already loaded from parquet */
          });
      }
    },
    [],
  );

  // Persist to sessionStorage on every change once we have an episode.
  useEffect(() => {
    if (episodeId == null) return;
    try {
      sessionStorage.setItem(
        storageKey(identKey(ident), episodeId),
        JSON.stringify(atoms),
      );
    } catch {
      /* ignore */
    }
    setDirty(JSON.stringify(atoms) !== savedSnapshotRef.current);
  }, [atoms, episodeId, ident]);

  const snap = useCallback(
    (ts: number) =>
      frameTimestamps.length > 0 ? snapToFrame(frameTimestamps, ts) : ts,
    [frameTimestamps],
  );

  const addAtom = useCallback(
    (atom: LanguageAtom) => {
      setAtoms((prev) => {
        recordBeforeChange(prev);
        return [...prev, atom];
      });
    },
    [recordBeforeChange],
  );

  const addAtoms = useCallback(
    (newAtoms: LanguageAtom[]) => {
      setAtoms((prev) => {
        recordBeforeChange(prev);
        return [...prev, ...newAtoms];
      });
    },
    [recordBeforeChange],
  );

  const updateAtom = useCallback(
    (index: number, updates: Partial<LanguageAtom>) => {
      setAtoms((prev) => {
        if (index < 0 || index >= prev.length) return prev;
        recordBeforeChange(prev);
        const next = prev.slice();
        next[index] = { ...next[index], ...updates };
        return next;
      });
    },
    [recordBeforeChange],
  );

  const deleteAtom = useCallback(
    (atom: LanguageAtom) => {
      setAtoms((prev) => {
        recordBeforeChange(prev);
        const next = prev.filter((a) => a !== atom);
        // If the deleted index was selected (or the selected index was after
        // the deleted one), nudge selection so it remains pointing at a
        // valid atom — or null when the list is empty.
        setSelectedIdxState((cur) => {
          if (cur == null) return null;
          const oldIdx = prev.indexOf(atom);
          if (oldIdx < 0) return cur;
          if (cur === oldIdx) return null;
          if (cur > oldIdx) return cur - 1;
          return cur;
        });
        return next;
      });
    },
    [recordBeforeChange],
  );

  const resetAtoms = useCallback(() => {
    setAtoms((prev) => {
      recordBeforeChange(prev);
      return [];
    });
    setSelectedIdxState(null);
  }, [recordBeforeChange]);

  // Undo/redo replace the atoms array wholesale — they bypass the mutators
  // above entirely (calling setAtoms directly), so they never re-enter
  // recordBeforeChange/redo-clearing themselves. Selection is kept when the
  // restored array is still long enough for it to resolve (the common case:
  // undoing a content/timestamp/`to` edit reverts an atom in place at the
  // same index, so the editor should stay open on it, not snap closed) and
  // dropped only when it can't possibly still mean anything.
  const undo = useCallback(() => {
    flushPendingGroup();
    const prev = historyRef.current.pop();
    if (prev === undefined) return;
    setAtoms((current) => {
      redoRef.current.push(current);
      if (redoRef.current.length > HISTORY_LIMIT) redoRef.current.shift();
      return prev;
    });
    // Most undos are a content/timestamp/`to` edit reverting in place — the
    // index is unchanged and still points at the same atom, so keep the
    // editor open on it rather than snapping it closed. Only drop the
    // selection when the restored array is too short for it to still mean
    // anything (e.g. undoing an add that was the last atom).
    setSelectedIdxState((cur) =>
      cur != null && cur < prev.length ? cur : null,
    );
  }, [flushPendingGroup]);

  const redo = useCallback(() => {
    const next = redoRef.current.pop();
    if (next === undefined) return;
    setAtoms((current) => {
      historyRef.current.push(current);
      if (historyRef.current.length > HISTORY_LIMIT) historyRef.current.shift();
      return next;
    });
    setSelectedIdxState((cur) =>
      cur != null && cur < next.length ? cur : null,
    );
  }, []);

  // Ctrl+Z / Cmd+Z undo, Ctrl+Shift+Z / Cmd+Shift+Z / Ctrl+Y redo — global
  // to the whole annotation surface (not scoped to one field's focus) and
  // always intercepted: controlled React inputs don't have a working
  // native undo history of their own to preserve (React overwrites the
  // DOM value on every keystroke), so there's nothing worth falling back
  // to — this app-level stack is a strict upgrade, and it's what keeps
  // sessionStorage/dirty tracking consistent with whatever the shortcut
  // just changed (those already react to `atoms`, so undo/redo need no
  // extra wiring there).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      const key = e.key.toLowerCase();
      if (key === "z" && e.shiftKey) {
        e.preventDefault();
        redo();
      } else if (key === "z") {
        e.preventDefault();
        undo();
      } else if (key === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [undo, redo]);

  const setPendingDraw = useCallback((draw: PendingDraw) => {
    setPendingDrawState(draw);
  }, []);

  const clearPendingDraw = useCallback(() => setPendingDrawState(null), []);

  const setActiveCamera = useCallback((c: string | null) => {
    setActiveCameraState(c);
  }, []);

  const setDrawMode = useCallback((m: DrawMode) => setDrawModeState(m), []);
  const setDrawLabel = useCallback((l: string) => setDrawLabelState(l), []);
  const setActiveVideoEl = useCallback(
    (el: HTMLVideoElement | null) => setActiveVideoElState(el),
    [],
  );

  const selectAtom = useCallback(
    (idx: number | null) => setSelectedIdxState(idx),
    [],
  );

  const save = useCallback(async (): Promise<{
    ok: boolean;
    error?: string;
    path?: string | null;
  }> => {
    if (episodeId == null) return { ok: false, error: "no episode" };
    if (!isAnnotateBackendEnabled()) {
      // Persistence is sessionStorage-only — that already happened in the
      // effect above. Report the storage key as the location so the UI can
      // show a concrete "path" instead of a vague offline message.
      savedSnapshotRef.current = JSON.stringify(atoms);
      setDirty(false);
      return {
        ok: true,
        path: `sessionStorage://${storageKey(identKey(ident), episodeId)}`,
      };
    }
    setSaving(true);
    try {
      const { path } = await saveEpisodeAtoms(episodeId, ident, atoms);
      savedSnapshotRef.current = JSON.stringify(atoms);
      setDirty(false);
      return { ok: true, path };
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    } finally {
      setSaving(false);
    }
  }, [atoms, episodeId, ident]);

  /**
   * Delete this episode's annotation file entirely and reset the in-memory
   * atoms back to empty — distinct from `save()` with an empty array, which
   * still records "reviewed, nothing to annotate". Reverts to the episode's
   * pristine, never-annotated state.
   */
  const deleteEpisodeFile = useCallback(async (): Promise<{
    ok: boolean;
    error?: string;
  }> => {
    if (episodeId == null) return { ok: false, error: "no episode" };
    setSaving(true);
    try {
      if (isAnnotateBackendEnabled()) {
        await deleteEpisodeAtoms(episodeId, ident);
      }
      try {
        sessionStorage.removeItem(storageKey(identKey(ident), episodeId));
      } catch {
        /* ignore */
      }
      setAtoms([]);
      setSelectedIdxState(null);
      savedSnapshotRef.current = "[]";
      setDirty(false);
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e instanceof Error ? e.message : String(e) };
    } finally {
      setSaving(false);
    }
  }, [episodeId, ident]);

  const flushAllEpisodes = useCallback(async (): Promise<void> => {
    if (!isAnnotateBackendEnabled()) return;
    const prefix = `${STORAGE_PREFIX}${identKey(ident)}::`;
    const pending: Promise<unknown>[] = [];
    for (let i = 0; i < sessionStorage.length; i++) {
      const key = sessionStorage.key(i);
      if (!key || !key.startsWith(prefix)) continue;
      const epIdx = Number(key.slice(prefix.length));
      // The currently open episode is already flushed by save() itself.
      if (!Number.isInteger(epIdx) || epIdx === episodeId) continue;
      let parsed: LanguageAtom[];
      try {
        parsed = JSON.parse(
          sessionStorage.getItem(key) || "[]",
        ) as LanguageAtom[];
      } catch {
        continue;
      }
      // Idempotent — a harmless no-op overwrite if the backend already has
      // this episode's current atoms — so no per-episode dirty tracking is
      // needed across navigations.
      pending.push(saveEpisodeAtoms(epIdx, ident, parsed).catch(() => {}));
    }
    await Promise.all(pending);
  }, [ident, episodeId]);

  const value = useMemo<AnnotationsContextType>(
    () => ({
      episodeId,
      ident,
      atoms,
      frameTimestamps,
      pendingDraw,
      activeCamera,
      activeVideoEl,
      setActiveVideoEl,
      drawMode,
      drawLabel,
      selectedIdx,
      selectAtom,
      backendEnabled,
      dirty,
      saving,
      setEpisode,
      setActiveCamera,
      setDrawMode,
      setDrawLabel,
      addAtom,
      addAtoms,
      updateAtom,
      deleteAtom,
      resetAtoms,
      setPendingDraw,
      clearPendingDraw,
      save,
      deleteEpisodeFile,
      flushAllEpisodes,
      undo,
      redo,
      snap,
    }),
    [
      episodeId,
      ident,
      atoms,
      frameTimestamps,
      pendingDraw,
      activeCamera,
      activeVideoEl,
      setActiveVideoEl,
      drawMode,
      drawLabel,
      selectedIdx,
      selectAtom,
      backendEnabled,
      dirty,
      saving,
      setEpisode,
      setActiveCamera,
      setDrawMode,
      setDrawLabel,
      addAtom,
      addAtoms,
      updateAtom,
      deleteAtom,
      resetAtoms,
      setPendingDraw,
      clearPendingDraw,
      save,
      deleteEpisodeFile,
      flushAllEpisodes,
      undo,
      redo,
      snap,
    ],
  );

  return (
    <T>
      {
        <AnnotationsContext.Provider value={value}>
          <T>{children}</T>
        </AnnotationsContext.Provider>
      }
    </T>
  );
};
