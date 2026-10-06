// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";
import { roundTo2 } from "@/components/viewer/time-format";
import { useConfirmAction } from "@/components/shell/confirm";
import {
  DatabaseZap,
  PanelRight,
  Play,
  Plus,
  Save,
  Trash2,
} from "lucide-react";
import { Button, IconButton } from "@/components/ds";
import {
  InspectorPortal,
  useInspectorReveal,
  useInspectorSlot,
} from "@/components/viewer/inspector";

import "@/components/viewer/annotations.css";

/**
 * Editor UI for v3.1 language atoms.
 *
 * Three vertical sections:
 *   1. Inline quick-add bar above the timeline (style picker + label + Add).
 *   2. Annotations timeline (in `annotations-timeline.tsx`).
 *   3. Workspace below the timeline:
 *        - Left rail: full atom list grouped by style; click to select.
 *        - Right pane: editor for the selected atom (or empty state).
 *
 * Bbox / keypoint VQA atoms are still added through the canvas overlay's
 * quick-label popup; the inline quick-add covers subtask / plan / memory /
 * interjection / speech / count / attribute / spatial.
 */

import React, { useId, useMemo, useState } from "react";
import { useTime } from "../context/time-context";
import { useAnnotations } from "../context/annotations-context";
import {
  buildSpeechAtom,
  classifyVqa,
  isSpeechAtom,
  parseVqaAnswer,
  speechText,
  type LanguageAtom,
} from "../types/language.types";
import {
  exportDataset as apiExport,
  fetchVocabulary,
  isAnnotateBackendEnabled,
  type Vocabulary,
} from "../utils/annotationsClient";
import {
  SubtaskTagFields,
  VocabularyEditor,
  withTag,
  type SubtaskTag,
} from "./subtask-vocabulary";
import { isSaveShortcut } from "../utils/keyboardShortcuts";
import { useDatasetSource } from "../context/dataset-source-context";
import { RawCaptureNotice } from "./raw-capture-notice";
import { ReadOnlyReason } from "./linked-dataset-notice";
import { LINKED_READ_ONLY } from "../utils/linkedDataset";

interface Props {
  cameraKeys: string[];
}

// Read-only readouts (list rows, timeline) keep three decimals: they show the
// exact stored time. The editable fields use `roundTo2` (two decimals) so the
// number fits the narrow inspector; see AtomEditor for why that is safe.
function formatSeconds(s: number): string {
  return s.toFixed(3) + "s";
}

function StylePill({ style }: { style: string | null }) {
  const cls = style ?? "speech";
  return (
    <T>{<span className={`style-pill ${cls}`}>{style ?? "speech"}</span>}</T>
  );
}

/**
 * Highlight a row when its timestamp is within ~half a frame of currentTime.
 */
function isActiveAt(ts: number, currentTime: number, fps = 30): boolean {
  return Math.abs(ts - currentTime) < 0.5 / fps;
}

type QuickAddKind =
  | "task_aug"
  | "subtask"
  | "plan"
  | "memory"
  | "interjection"
  | "speech"
  | "count"
  | "attribute"
  | "spatial";

interface QuickAddField {
  name: string;
  placeholder: string;
  type?: "text" | "number";
  width?: string;
  grow?: boolean;
}

interface QuickAddBuildCtx {
  ts: number;
  vqaCamera: string | null;
}

interface QuickAddDef {
  kind: QuickAddKind;
  label: string;
  /** When true, the displayed timestamp is 0 (atom is pinned to episode start). */
  atEpisodeStart?: boolean;
  fields: QuickAddField[];
  build: (
    values: Record<string, string>,
    ctx: QuickAddBuildCtx,
  ) => LanguageAtom[] | null;
}

// Each text-style atom kind (and the simpler VQA shapes) is one entry: how
// it appears in the dropdown, what fields the user fills, and how those
// values map to one or two language atoms.
const QUICK_ADD_DEFS: QuickAddDef[] = [
  {
    kind: "task_aug",
    label: "task augmentation",
    atEpisodeStart: true,
    fields: [
      {
        name: "label",
        placeholder: "pick up the blue cube and place it in the green box",
        grow: true,
      },
    ],
    build: ({ label }) => {
      const text = label.trim();
      if (!text) return null;
      return [
        {
          role: "user",
          content: text,
          style: "task_aug",
          timestamp: 0,
          camera: null,
          tool_calls: null,
        },
      ];
    },
  },
  {
    kind: "subtask",
    label: "subtask",
    fields: [
      {
        name: "label",
        placeholder: "grasp the handle of the sponge",
        grow: true,
      },
    ],
    build: ({ label }, { ts }) => {
      const text = label.trim();
      if (!text) return null;
      return [
        {
          role: "assistant",
          content: text,
          style: "subtask",
          timestamp: ts,
          camera: null,
          tool_calls: null,
        },
      ];
    },
  },
  {
    kind: "plan",
    label: "plan",
    fields: [
      {
        name: "label",
        placeholder: "1. grab sponge / 2. wipe / 3. tidy",
        grow: true,
      },
    ],
    build: ({ label }, { ts }) => {
      const text = label.trim();
      if (!text) return null;
      return [
        {
          role: "assistant",
          content: text,
          style: "plan",
          timestamp: ts,
          camera: null,
          tool_calls: null,
        },
      ];
    },
  },
  {
    kind: "memory",
    label: "memory",
    fields: [
      {
        name: "label",
        placeholder: "sponge picked up; counter still dirty",
        grow: true,
      },
    ],
    build: ({ label }, { ts }) => {
      const text = label.trim();
      if (!text) return null;
      return [
        {
          role: "assistant",
          content: text,
          style: "memory",
          timestamp: ts,
          camera: null,
          tool_calls: null,
        },
      ];
    },
  },
  {
    kind: "interjection",
    label: "interjection (user)",
    fields: [
      {
        name: "label",
        placeholder: "user: actually skip the wipe…",
        grow: true,
      },
    ],
    build: ({ label }, { ts }) => {
      const text = label.trim();
      if (!text) return null;
      return [
        {
          role: "user",
          content: text,
          style: "interjection",
          timestamp: ts,
          camera: null,
          tool_calls: null,
        },
      ];
    },
  },
  {
    kind: "speech",
    label: "speech (robot say)",
    fields: [
      {
        name: "label",
        placeholder: "robot say: Got it, skipping the wipe.",
        grow: true,
      },
    ],
    build: ({ label }, { ts }) => {
      const text = label.trim();
      if (!text) return null;
      return [buildSpeechAtom(ts, text)];
    },
  },
  {
    kind: "count",
    label: "vqa: count",
    fields: [
      { name: "label", placeholder: "object label (e.g. cup)", grow: true },
      { name: "count", placeholder: "count", type: "number", width: "80px" },
    ],
    build: ({ label, count }, { ts, vqaCamera }) => {
      const text = label.trim();
      if (!text || !count) return null;
      return [
        {
          role: "user",
          content: `How many ${text}?`,
          style: "vqa",
          timestamp: ts,
          camera: vqaCamera,
          tool_calls: null,
        },
        {
          role: "assistant",
          content: JSON.stringify({ label: text, count: Number(count) }),
          style: "vqa",
          timestamp: ts,
          camera: vqaCamera,
          tool_calls: null,
        },
      ];
    },
  },
  {
    kind: "attribute",
    label: "vqa: attribute",
    fields: [
      { name: "label", placeholder: "label", width: "120px" },
      { name: "attribute", placeholder: "attribute (color)", width: "120px" },
      { name: "value", placeholder: "value (red)", grow: true },
    ],
    build: ({ label, attribute, value }, { ts, vqaCamera }) => {
      const text = label.trim();
      if (!text || !attribute || !value) return null;
      return [
        {
          role: "user",
          content: `What ${attribute} is the ${text}?`,
          style: "vqa",
          timestamp: ts,
          camera: vqaCamera,
          tool_calls: null,
        },
        {
          role: "assistant",
          content: JSON.stringify({ label: text, attribute, value }),
          style: "vqa",
          timestamp: ts,
          camera: vqaCamera,
          tool_calls: null,
        },
      ];
    },
  },
  {
    kind: "spatial",
    label: "vqa: spatial relation",
    fields: [
      { name: "subject", placeholder: "subject", width: "100px" },
      { name: "relation", placeholder: "relation (right_of)", width: "130px" },
      { name: "object", placeholder: "object", grow: true },
    ],
    build: ({ subject, relation, object }, { ts, vqaCamera }) => {
      if (!subject || !relation || !object) return null;
      return [
        {
          role: "user",
          content: `Where is the ${subject} relative to the ${object}?`,
          style: "vqa",
          timestamp: ts,
          camera: vqaCamera,
          tool_calls: null,
        },
        {
          role: "assistant",
          content: JSON.stringify({ subject, relation, object }),
          style: "vqa",
          timestamp: ts,
          camera: vqaCamera,
          tool_calls: null,
        },
      ];
    },
  },
];

const QUICK_ADD_DEFS_BY_KIND: Record<QuickAddKind, QuickAddDef> =
  QUICK_ADD_DEFS.reduce(
    (acc, def) => {
      acc[def.kind] = def;
      return acc;
    },
    {} as Record<QuickAddKind, QuickAddDef>,
  );

interface RailGroupDef {
  key: string;
  title: string;
  dotClass: string;
  // Which v3.1 language column this style is written to. Used to group the
  // rail under "Persistent" vs "Events" headers so it's clear at a glance
  // that task_aug / subtask / plan / memory broadcast across the whole
  // episode (language_persistent) while interjection / speech / vqa fire on
  // a single frame (language_events). Mirrors columnForStyle() exactly.
  column: "persistent" | "events";
  match: (
    atom: LanguageAtom,
    otherCamera: (a: LanguageAtom) => boolean,
  ) => boolean;
  label: (
    atom: LanguageAtom,
    helpers: {
      activeCamera: string | null;
      firstLine: (s: string | null) => string;
    },
  ) => string;
}

const RAIL_GROUPS: RailGroupDef[] = [
  {
    key: "task_aug",
    title: "task aug",
    dotClass: "dot-task-aug",
    column: "persistent",
    match: (a) => a.style === "task_aug",
    label: (a) => a.content || "(empty)",
  },
  {
    key: "subtask",
    title: "subtask",
    dotClass: "dot-subtask",
    column: "persistent",
    match: (a) => a.style === "subtask",
    label: (a) => a.content || "(empty)",
  },
  {
    key: "plan",
    title: "plan",
    dotClass: "dot-plan",
    column: "persistent",
    match: (a) => a.style === "plan",
    label: (a, { firstLine }) => firstLine(a.content),
  },
  {
    key: "memory",
    title: "memory",
    dotClass: "dot-memory",
    column: "persistent",
    match: (a) => a.style === "memory",
    label: (a, { firstLine }) => firstLine(a.content),
  },
  {
    key: "interjection",
    title: "interjection",
    dotClass: "dot-interjection",
    column: "events",
    match: (a) => a.style === "interjection",
    label: (a) => a.content || "(empty)",
  },
  {
    key: "speech",
    title: "speech",
    dotClass: "dot-speech",
    column: "events",
    match: (a) => isSpeechAtom(a),
    label: (a) => speechText(a) || "(empty)",
  },
  {
    key: "vqa",
    title: "vqa",
    dotClass: "dot-vqa",
    column: "events",
    match: (a, otherCamera) => a.style === "vqa" && !otherCamera(a),
    label: (a, { activeCamera }) => {
      const role = a.role === "user" ? "Q" : "A";
      const t = a.content || "";
      const cameraSuffix =
        a.camera && a.camera !== activeCamera ? `  [${a.camera}]` : "";
      return `${role}: ${t.slice(0, 60)}${t.length > 60 ? "…" : ""}${cameraSuffix}`;
    },
  },
];

function useJump(): (ts: number) => void {
  const { seek, setIsPlaying } = useTime();
  return React.useCallback(
    (ts: number) => {
      seek(ts, "external");
      setIsPlaying(false);
    },
    [seek, setIsPlaying],
  );
}

/**
 * Ctrl+S commits one in-progress annotation draft — the quick-add form's
 * typed-but-not-added fields, or a selected atom's pending field edits —
 * into the local `atoms` array. When there is no local draft, the panel-level
 * handler calls save() instead, so Ctrl/Cmd+S persists committed edits and
 * prevents the browser's Save Page dialog. Draft commits remain separate
 * from backend writes.
 *
 * Escape discards the draft the same way.
 *
 * Both the quick-add form and the atom editor register this independently.
 * In the common case only one ever has a draft (quick-add fields empty
 * while an atom is being edited, or vice versa); if both happen to have one
 * at once, both fire — an accepted edge case rather than a focus-tracking
 * system that adds real complexity for a rare case.
 */
function useAnnotationDraftShortcuts({
  hasDraft,
  onCommit,
  onCancel,
}: {
  hasDraft: boolean;
  onCommit: () => void;
  onCancel: () => void;
}): void {
  React.useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      if (isSaveShortcut(e)) {
        if (!hasDraft) return;
        e.preventDefault();
        onCommit();
        return;
      }
      if (e.key === "Escape" && hasDraft) {
        e.preventDefault();
        onCancel();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [hasDraft, onCommit, onCancel]);
}

export const AnnotationsPanel: React.FC<Props> = ({ cameraKeys }) => {
  const {
    atoms,
    addAtoms,
    updateAtom,
    deleteAtom,
    snap,
    save,
    deleteEpisodeFile,
    flushAllEpisodes,
    saving,
    dirty,
    backendEnabled,
    activeCamera,
    setActiveCamera,
    setDrawMode,
    selectedIdx,
    selectAtom,
    ident,
    readOnly,
  } = useAnnotations();
  const { currentTime } = useTime();
  const { t } = useLocale();
  const confirm = useConfirmAction();
  // In the episode viewer the inspector form lives in the right column.
  const inspectorDocked = useInspectorSlot() !== null;
  const revealInspector = useInspectorReveal();

  // ============ Inline quick-add state ============
  const [qaKind, setQaKind] = useState<QuickAddKind>("subtask");
  const [qaValues, setQaValues] = useState<Record<string, string>>({});
  const [exportStatus, setExportStatus] = useState<string | null>(null);
  const { isRaw } = useDatasetSource();
  const [showExportHint, setShowExportHint] = useState(false);
  const qaDef = QUICK_ADD_DEFS_BY_KIND[qaKind];
  // The dataset's subtask vocabulary, and the tag the next subtask gets.
  const [vocabulary, setVocabulary] = useState<Vocabulary>({
    subtasks: [],
    updated_at: null,
  });
  const [qaTag, setQaTag] = useState<SubtaskTag>({});
  const identKey = `${ident.repoId ?? ""}|${ident.localPath ?? ""}`;
  React.useEffect(() => {
    if (!backendEnabled || (!ident.repoId && !ident.localPath)) return;
    let cancelled = false;
    fetchVocabulary(ident)
      .then((value) => {
        if (!cancelled) setVocabulary(value);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identKey, backendEnabled]);

  // Initialize active camera once cameras arrive.
  React.useEffect(() => {
    if (!activeCamera && cameraKeys.length > 0) setActiveCamera(cameraKeys[0]);
  }, [activeCamera, cameraKeys, setActiveCamera]);

  // The Annotations tab keeps the canvas overlay in "auto" mode the whole
  // time — drag = bbox, click = keypoint.
  React.useEffect(() => {
    setDrawMode("auto");
    return () => setDrawMode("off");
  }, [setDrawMode]);

  // ============ Atom grouping for the rail ============
  // The rail shows one section per atom-kind. Each kind is a single config
  // entry: how to detect atoms in this kind, and how to label them in the row.
  // VQA filters out other-camera answers when the dataset has multiple
  // cameras so the rail mirrors the active video.
  const groups = useMemo(() => {
    const firstLine = (s: string | null) =>
      (s || "").split("\n")[0] || "(empty)";
    const otherCamera = (a: LanguageAtom): boolean =>
      !!activeCamera &&
      cameraKeys.length > 1 &&
      a.camera != null &&
      a.camera !== activeCamera;
    return RAIL_GROUPS.map((def) => {
      const entries = atoms
        .map((atom, idx) => ({ atom, idx }))
        .filter(({ atom }) => def.match(atom, otherCamera))
        .map(({ atom, idx }) => ({
          atom,
          idx,
          label: def.label(atom, { activeCamera, firstLine }),
        }))
        .sort((a, b) => a.atom.timestamp - b.atom.timestamp);
      return { def, entries };
    });
  }, [atoms, activeCamera, cameraKeys.length]);

  // ============ Quick-add handler ============
  // VQA quick-adds inherit the active camera so per-camera filtering shows
  // them in the right rail / overlay. Non-VQA atoms stay camera-agnostic
  // (the def's `build` ignores `vqaCamera` for those).
  const handleQuickAdd = () => {
    const ts = snap(currentTime);
    const vqaCamera = activeCamera ?? cameraKeys[0] ?? null;
    const tagged = qaKind === "subtask" && (qaTag.subtask_id || qaTag.outcome);
    // A subtask picked from the vocabulary needs no typed description.
    const label =
      qaValues.label?.trim() ||
      (qaKind === "subtask" && qaTag.subtask_id
        ? (vocabulary.subtasks.find((s) => s.id === qaTag.subtask_id)?.label ??
          qaTag.subtask_id)
        : "");
    const newAtoms = qaDef.build(
      { ...qaValues, ...(qaKind === "subtask" ? { label } : {}) },
      { ts, vqaCamera },
    );
    if (!newAtoms || !newAtoms.length) return;
    if (tagged) newAtoms[0] = { ...newAtoms[0], levi: withTag(null, qaTag) };
    addAtoms(newAtoms);
    // Select the freshly added atom (last one added) so the editor opens for it.
    selectAtom(atoms.length + newAtoms.length - 1);
    setQaValues({});
    setQaTag({});
  };

  const qaHasDraft = Object.values(qaValues).some((v) => v.trim() !== "");
  useAnnotationDraftShortcuts({
    hasDraft: qaHasDraft,
    onCommit: handleQuickAdd,
    onCancel: () => setQaValues({}),
  });

  // ============ Save / export ============
  const handleSave = React.useCallback(async () => {
    if (readOnly) {
      setExportStatus(LINKED_READ_ONLY);
      return;
    }
    const r = await save();
    if (!r.ok) {
      setExportStatus(`Save failed: ${r.error || "unknown"}`);
    } else {
      setExportStatus(
        r.path
          ? `Saved episode to ${r.path}`
          : "Saved episode (backend did not report a path — update/restart backend/app.py).",
      );
    }
  }, [save, readOnly]);

  // Ctrl/Cmd+S saves the current episode when no field has a local draft.
  React.useEffect(() => {
    if (readOnly) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (!isSaveShortcut(e) || e.defaultPrevented) return;
      const target = e.target;
      if (
        target instanceof HTMLElement &&
        target.closest('[data-annotation-draft-active="true"], .quick-popup')
      ) {
        return;
      }
      e.preventDefault();
      if (!saving) void handleSave();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [handleSave, saving, readOnly]);

  const handleSaveDataset = async () => {
    if (isRaw) {
      // A raw capture's view is not a dataset to export: explain instead.
      setShowExportHint(true);
      return;
    }
    if (!isAnnotateBackendEnabled()) {
      setExportStatus(
        "Backend not configured. Set NEXT_PUBLIC_ANNOTATE_BACKEND_URL and run backend/app.py.",
      );
      return;
    }
    setExportStatus("Saving dataset…");
    try {
      const saved = await save();
      if (!saved.ok) throw new Error(saved.error || "Save failed");
      // Other episodes edited earlier this session but never explicitly
      // "Save episode"'d would otherwise be silently absent from the export
      // — only the currently open episode is guaranteed fresh by save().
      await flushAllEpisodes();
      const r = await apiExport(ident);
      setExportStatus(
        r.reused_existing_export
          ? `Updated existing export at ${r.output_dir} (persistent: ${r.persistent_rows}, events: ${r.event_rows}).`
          : `Saved dataset to ${r.output_dir} (persistent: ${r.persistent_rows}, events: ${r.event_rows}).`,
      );
    } catch (e) {
      setExportStatus(
        `Save dataset failed: ${e instanceof Error ? e.message : String(e)}`,
      );
    }
  };

  const handleDeleteFile = async () => {
    if (
      !(await confirm({
        title: t("Delete this episode's annotation file?"),
        description: t(
          "Delete this episode's annotation file? This removes every saved atom for this episode and cannot be undone.",
        ),
        confirmLabel: t("Delete"),
        tone: "danger",
      }))
    ) {
      return;
    }
    const r = await deleteEpisodeFile();
    setExportStatus(
      r.ok
        ? "Deleted this episode's annotation file."
        : `Delete failed: ${r.error || "unknown"}`,
    );
  };

  const selectedAtom =
    selectedIdx != null && selectedIdx >= 0 && selectedIdx < atoms.length
      ? atoms[selectedIdx]
      : null;

  // ============ Render ============
  return (
    <T>
      {
        <div className="annotation-workbench">
          <div className="annotation-actionbar">
            <div>
              <h3>
                <T>Language annotations</T>
                {dirty && (
                  <span className="dirty-pill">
                    <T>unsaved</T>
                  </span>
                )}
              </h3>
              <p>
                <T>
                  {readOnly
                    ? "Select an atom from the timeline or list to read it in the inspector."
                    : "Select an atom from the timeline or list, then edit it in the inspector."}
                </T>
              </p>
            </div>
            <div className="actionbar-actions">
              {!backendEnabled && (
                <span className="backend-offline">
                  <T>backend offline — edits saved to sessionStorage only</T>
                </span>
              )}
              {readOnly ? (
                <ReadOnlyReason />
              ) : (
                <>
                  <Button
                    variant="primary"
                    size="sm"
                    icon={Save}
                    loading={saving}
                    disabled={!dirty}
                    onClick={handleSave}
                    aria-keyshortcuts="Control+S Meta+S"
                  >
                    {t(saving ? "Saving…" : "Save episode")}
                  </Button>
                  <Button
                    size="sm"
                    icon={DatabaseZap}
                    disabled={!backendEnabled}
                    onClick={handleSaveDataset}
                  >
                    {t("Save dataset")}
                  </Button>
                  <Button
                    variant="secondary"
                    className="vw-btn-danger-outline"
                    size="sm"
                    icon={Trash2}
                    disabled={!backendEnabled || saving}
                    onClick={handleDeleteFile}
                  >
                    {t("Delete file")}
                  </Button>
                </>
              )}
            </div>
          </div>

          {isRaw && showExportHint && <RawCaptureNotice feature="export" />}
          {exportStatus && (
            <div className="save-status" role="status">
              <T>{exportStatus}</T>
            </div>
          )}

          {!readOnly && (
            <section className="annotation-composer">
              <div className="composer-copy">
                <span className="section-kicker">
                  <T>Add text annotation</T>
                </span>
                <p>
                  <T>
                    Adds task phrasing, subtask, plan, memory, speech, or
                    non-spatial VQA atoms. Task phrasings are saved at episode
                    start.
                  </T>
                </p>
              </div>
              <div
                className="quick-add"
                data-annotation-draft-active={qaHasDraft ? "true" : "false"}
              >
                <span className="ts-pill">
                  t ={" "}
                  <T>
                    {qaDef.atEpisodeStart
                      ? formatSeconds(0)
                      : formatSeconds(currentTime)}
                  </T>
                </span>
                <select
                  aria-label="Annotation kind"
                  value={qaKind}
                  onChange={(e) => {
                    setQaKind(e.target.value as QuickAddKind);
                    setQaValues({});
                  }}
                >
                  {QUICK_ADD_DEFS.map((d) => (
                    <option key={d.kind} value={d.kind}>
                      <T>{d.label}</T>
                    </option>
                  ))}
                </select>
                {qaKind === "subtask" && (
                  <SubtaskTagFields
                    vocabulary={vocabulary}
                    value={qaTag}
                    onChange={setQaTag}
                  />
                )}
                {qaDef.fields.map((f, i) => (
                  <input
                    key={f.name}
                    type={f.type === "number" ? "number" : "text"}
                    placeholder={t(f.placeholder)}
                    className={f.grow ? "grow" : undefined}
                    style={f.width ? { width: f.width } : undefined}
                    value={qaValues[f.name] ?? ""}
                    onChange={(e) =>
                      setQaValues((v) => ({ ...v, [f.name]: e.target.value }))
                    }
                    onKeyDown={
                      i === qaDef.fields.length - 1
                        ? (e) => e.key === "Enter" && handleQuickAdd()
                        : undefined
                    }
                  />
                ))}
                <Button
                  variant="secondary"
                  size="sm"
                  icon={Plus}
                  onClick={handleQuickAdd}
                >
                  {t("Add at frame")}
                </Button>
              </div>
              {backendEnabled && (
                <VocabularyEditor
                  ident={ident}
                  vocabulary={vocabulary}
                  onSaved={setVocabulary}
                />
              )}
            </section>
          )}

          <div
            className={`workspace inspector-workspace${inspectorDocked ? " is-docked" : ""}`}
          >
            <div className="rail annotation-list">
              <div className="list-head">
                <div>
                  <span className="section-kicker">
                    <T>Annotations</T>
                  </span>
                  <p>
                    <T>{atoms.length}</T>
                    <T> atoms in this episode</T>
                  </p>
                </div>
                <span className="flex items-center gap-2">
                  {revealInspector && selectedAtom && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={PanelRight}
                      onClick={revealInspector}
                    >
                      {t("Go to inspector")}
                    </Button>
                  )}
                  <span className="ts-pill">{formatSeconds(currentTime)}</span>
                </span>
              </div>
              {atoms.length === 0 && (
                <div className="rail-empty">
                  <T>No annotations yet.</T>
                  {!readOnly && (
                    <>
                      <br />
                      <T>Add text above or draw on the active video.</T>
                    </>
                  )}
                </div>
              )}
              {(["persistent", "events"] as const).map((column) => {
                const colGroups = groups.filter(
                  ({ def }) => def.column === column,
                );
                const total = colGroups.reduce(
                  (n, { entries }) => n + entries.length,
                  0,
                );
                if (total === 0) return null;
                return (
                  <div className="rail-column" key={column}>
                    <div className={`rail-column-head ${column}`}>
                      <span className="rail-column-title">
                        <T>
                          {column === "persistent" ? "Persistent" : "Events"}
                        </T>
                      </span>
                      <span className="rail-column-sub">
                        <T>
                          {column === "persistent"
                            ? "language_persistent · broadcast across every frame"
                            : "language_events · fire on a single frame"}
                        </T>
                      </span>
                    </div>
                    {colGroups.map(({ def, entries }) => (
                      <RailGroup
                        key={def.key}
                        title={def.title}
                        dotClass={def.dotClass}
                        entries={entries}
                        currentTime={currentTime}
                      />
                    ))}
                  </div>
                );
              })}
            </div>

            <InspectorPortal>
              <div className="editor inspector">
                <T>
                  {selectedAtom == null ? (
                    <div className="editor-empty">
                      <span className="section-kicker">
                        <T>Inspector</T>
                      </span>
                      <p>
                        {readOnly
                          ? "Select an annotation from the list or timeline to read it here."
                          : "Select an annotation from the list or timeline, or draw a new bbox/keypoint on the video."}
                      </p>
                    </div>
                  ) : readOnly ? (
                    <ReadOnlyAtom atom={selectedAtom} />
                  ) : (
                    <AtomEditor
                      atom={selectedAtom}
                      cameraKeys={cameraKeys}
                      vocabulary={vocabulary}
                      onChange={(updates) =>
                        updateAtom(selectedIdx as number, updates)
                      }
                      onDelete={() => deleteAtom(selectedAtom)}
                    />
                  )}
                </T>
              </div>
            </InspectorPortal>
          </div>
        </div>
      }
    </T>
  );
};

// ---------------------------------------------------------------------------
// Rail group — one row per atom, click selects.
// ---------------------------------------------------------------------------

const RailGroup: React.FC<{
  title: string;
  dotClass: string;
  entries: { atom: LanguageAtom; idx: number; label: string }[];
  currentTime: number;
}> = ({ title, dotClass, entries, currentTime }) => {
  const { selectedIdx, selectAtom } = useAnnotations();
  const jump = useJump();
  const revealInspector = useInspectorReveal();
  if (entries.length === 0) return null;
  return (
    <T>
      {
        <div className="rail-group">
          <div className="rail-group-head">
            <span
              style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              <span className={`style-dot ${dotClass}`} />
              <T>{title}</T>
            </span>
            <span className="count">
              <T>{entries.length}</T>
            </span>
          </div>
          {entries.map(({ atom, idx, label }) => {
            const sel = idx === selectedIdx;
            const active = isActiveAt(atom.timestamp, currentTime);
            return (
              <div
                key={idx}
                className={`rail-row ${sel ? "selected" : ""} ${active ? "active-now" : ""}`}
                role="button"
                tabIndex={0}
                aria-pressed={sel}
                onClick={() => {
                  selectAtom(idx);
                  jump(atom.timestamp);
                }}
                onKeyDown={(event) => {
                  // Enter or Space selects, as a click does; Enter also
                  // takes the keyboard to the inspector form.
                  if (event.key !== "Enter" && event.key !== " ") return;
                  event.preventDefault();
                  // Keep Space from also toggling playback (window key).
                  event.stopPropagation();
                  selectAtom(idx);
                  jump(atom.timestamp);
                  if (event.key === "Enter") revealInspector?.();
                }}
              >
                <span className="ts">{formatSeconds(atom.timestamp)}</span>
                <span className="body">
                  <T>{label}</T>
                </span>
              </div>
            );
          })}
        </div>
      }
    </T>
  );
};

// ---------------------------------------------------------------------------
// ReadOnlyAtom — the selected atom of a linked (live workspace) dataset: what
// AtomEditor shows, without a field to change.
// ---------------------------------------------------------------------------

export const ReadOnlyAtom: React.FC<{ atom: LanguageAtom }> = ({ atom }) => {
  const { t } = useLocale();
  const jump = useJump();
  const roleLabel = isSpeechAtom(atom) ? "speech" : atom.role;
  const cameraLabel = atom.camera ?? "all cameras";
  const rows: { label: string; value: string }[] = [
    {
      label: "Timestamp (s)",
      value: `${roundTo2(atom.timestamp)}${
        atom.to != null && atom.to > atom.timestamp
          ? ` → ${roundTo2(atom.to)}`
          : ""
      }`,
    },
  ];
  if (atom.levi?.subtask_id)
    rows.push({ label: "Subtask ID", value: atom.levi.subtask_id });
  if (atom.levi?.outcome)
    rows.push({
      label: "Outcome",
      value: t(
        atom.levi.outcome === "success"
          ? "Success"
          : atom.levi.outcome === "failure"
            ? "Failure"
            : "Unknown",
      ),
    });
  if (atom.levi?.attempt != null)
    rows.push({ label: "Attempt", value: String(atom.levi.attempt) });
  if (atom.content) rows.push({ label: "Content", value: atom.content });
  return (
    <div className="inspector-body">
      <div className="editor-head inspector-head">
        <div className="inspector-title">
          <StylePill style={atom.style} />
          <div>
            <strong>
              {formatSeconds(atom.timestamp)}
              {atom.to != null && atom.to > atom.timestamp
                ? ` → ${formatSeconds(atom.to)}`
                : ""}
            </strong>
            <span>
              <T>{roleLabel}</T> · <T>{cameraLabel}</T>
            </span>
          </div>
        </div>
        <div className="right">
          <IconButton
            icon={Play}
            size="sm"
            label={t("Jump to this atom's frame")}
            onClick={() => jump(atom.timestamp)}
          />
        </div>
      </div>
      <dl className="vw-readonly-fields">
        {rows.map((row) => (
          <div className="field" key={row.label}>
            <dt className="field-label">{t(row.label)}</dt>
            <dd className="vw-readonly-value">{row.value}</dd>
          </div>
        ))}
      </dl>
      <ReadOnlyReason />
    </div>
  );
};

// ---------------------------------------------------------------------------
// AtomEditor — form for the currently selected atom.
// ---------------------------------------------------------------------------

export const AtomEditor: React.FC<{
  atom: LanguageAtom;
  cameraKeys: string[];
  vocabulary: Vocabulary;
  onChange: (updates: Partial<LanguageAtom>) => void;
  onDelete: () => void;
}> = ({ atom, cameraKeys, vocabulary, onChange, onDelete }) => {
  const { t } = useLocale();
  const uid = useId();
  const jump = useJump();
  const { snap } = useAnnotations();
  const isSpeech = isSpeechAtom(atom);
  const cameraLabel = atom.camera ?? "all cameras";
  const roleLabel = isSpeech ? "speech" : atom.role;
  const [timestampDraft, setTimestampDraft] = useState(() =>
    roundTo2(atom.timestamp),
  );
  const [toDraft, setToDraft] = useState(() =>
    atom.to != null ? roundTo2(atom.to) : "",
  );

  React.useEffect(() => {
    setTimestampDraft(roundTo2(atom.timestamp));
  }, [atom.timestamp]);
  React.useEffect(() => {
    setToDraft(atom.to != null ? roundTo2(atom.to) : "");
  }, [atom.to]);

  const commitTimestamp = React.useCallback(
    (raw = timestampDraft) => {
      // The field shows the time rounded to 0.01 s. Text equal to that
      // display is "unchanged": keep the exact stored time instead of
      // rewriting it with the rounded one. (Deliberately typing the same
      // number as the display, e.g. 1.23 over a stored 1.23456, therefore
      // writes nothing; type another value, or use "snap to frame".)
      if (raw === roundTo2(atom.timestamp)) return;
      const next = Number(raw);
      if (!Number.isFinite(next) || next < 0) {
        setTimestampDraft(roundTo2(atom.timestamp));
        return;
      }
      onChange({ timestamp: next });
      setTimestampDraft(roundTo2(next));
    },
    [atom.timestamp, onChange, timestampDraft],
  );

  const commitSnappedTimestamp = () => {
    // Snap from the stored time when the draft is untouched: the rounded
    // display could land next to the wrong frame at high frame rates.
    const parsed =
      timestampDraft === roundTo2(atom.timestamp)
        ? atom.timestamp
        : Number(timestampDraft);
    const next = snap(Number.isFinite(parsed) ? parsed : atom.timestamp);
    onChange({ timestamp: next });
    setTimestampDraft(roundTo2(next));
  };

  // Optional range end — LEVI-only editorial metadata (never exported into
  // the lerobot struct, see `LanguageAtom.to`). Blank clears the range back
  // to a point-in-time atom.
  const commitTo = React.useCallback(
    (raw = toDraft) => {
      const trimmed = raw.trim();
      if (trimmed === "") {
        onChange({ to: null });
        setToDraft("");
        return;
      }
      if (atom.to != null && trimmed === roundTo2(atom.to)) return;
      const next = Number(trimmed);
      if (!Number.isFinite(next) || next < atom.timestamp) {
        setToDraft(atom.to != null ? roundTo2(atom.to) : "");
        return;
      }
      onChange({ to: next });
      setToDraft(roundTo2(next));
    },
    [atom.timestamp, atom.to, onChange, toDraft],
  );

  const commitSnappedTo = () => {
    const parsed =
      atom.to != null && toDraft === roundTo2(atom.to)
        ? atom.to
        : Number(toDraft);
    const base = Number.isFinite(parsed) ? parsed : (atom.to ?? atom.timestamp);
    const next = Math.max(atom.timestamp, snap(base));
    onChange({ to: next });
    setToDraft(roundTo2(next));
  };

  // Content edits (the textarea below) commit on every keystroke — there's
  // nothing pending there. Timestamp/`to` are the only fields with a
  // draft-then-commit pattern, so they're the only ones a Ctrl+S/Escape
  // shortcut needs to resolve.
  const committedToStr = atom.to != null ? roundTo2(atom.to) : "";
  const hasDraft =
    timestampDraft !== roundTo2(atom.timestamp) || toDraft !== committedToStr;
  useAnnotationDraftShortcuts({
    hasDraft,
    onCommit: () => {
      commitTimestamp();
      commitTo();
    },
    onCancel: () => {
      setTimestampDraft(roundTo2(atom.timestamp));
      setToDraft(committedToStr);
    },
  });

  return (
    <T>
      {
        <div
          className="inspector-body"
          data-annotation-draft-active={hasDraft ? "true" : "false"}
        >
          <div className="editor-head inspector-head">
            <div className="inspector-title">
              <StylePill style={atom.style} />
              <div>
                <strong>
                  {formatSeconds(atom.timestamp)}
                  {atom.to != null && atom.to > atom.timestamp
                    ? ` → ${formatSeconds(atom.to)}`
                    : ""}
                </strong>
                <span>
                  <T>{roleLabel}</T> · <T>{cameraLabel}</T>
                </span>
              </div>
            </div>
            <div className="right">
              <IconButton
                icon={Play}
                size="sm"
                label={t("Jump to this atom's frame")}
                onClick={() => jump(atom.timestamp)}
              />
              <IconButton
                icon={Trash2}
                size="sm"
                label={t("Delete this atom")}
                onClick={onDelete}
              />
            </div>
          </div>

          {atom.style === "subtask" && vocabulary.subtasks.length > 0 && (
            <div className="field">
              {/* Two selects share this name: a group, not a label. */}
              <span className="field-label" id={`${uid}-subtask`}>
                <T>Subtask and outcome</T>
              </span>
              <div
                className="ts-row"
                role="group"
                aria-labelledby={`${uid}-subtask`}
              >
                <SubtaskTagFields
                  vocabulary={vocabulary}
                  value={{
                    subtask_id: atom.levi?.subtask_id ?? null,
                    outcome: atom.levi?.outcome ?? null,
                  }}
                  onChange={(tag) =>
                    onChange({ levi: withTag(atom.levi, tag) })
                  }
                />
              </div>
            </div>
          )}

          <div className="field">
            <label className="field-label" htmlFor={`${uid}-timestamp`}>
              <T>Timestamp (s)</T>
            </label>
            <div className="ts-row">
              <input
                id={`${uid}-timestamp`}
                type="text"
                inputMode="decimal"
                value={timestampDraft}
                onChange={(e) => setTimestampDraft(e.target.value)}
                onBlur={() => commitTimestamp()}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitTimestamp();
                  if (e.key === "Escape")
                    setTimestampDraft(roundTo2(atom.timestamp));
                }}
              />
              <Button
                size="sm"
                variant="ghost"
                onPointerDown={(e) => {
                  e.preventDefault();
                  commitSnappedTimestamp();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    commitSnappedTimestamp();
                  }
                }}
              >
                {t("snap to frame")}
              </Button>
            </div>
          </div>

          {/* Optional range end. task_aug always spans the whole episode, so
          a range is meaningless there. */}
          {atom.style !== "task_aug" && (
            <div className="field">
              <label className="field-label" htmlFor={`${uid}-to`}>
                <T>To (s) — optional range end</T>
              </label>
              <div className="ts-row">
                <input
                  id={`${uid}-to`}
                  type="text"
                  inputMode="decimal"
                  placeholder={t("point in time")}
                  value={toDraft}
                  onChange={(e) => setToDraft(e.target.value)}
                  onBlur={() => commitTo()}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitTo();
                    if (e.key === "Escape")
                      setToDraft(atom.to != null ? roundTo2(atom.to) : "");
                  }}
                />
                <Button
                  size="sm"
                  variant="ghost"
                  onPointerDown={(e) => {
                    e.preventDefault();
                    commitSnappedTo();
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      commitSnappedTo();
                    }
                  }}
                >
                  {t("snap to frame")}
                </Button>
              </div>
            </div>
          )}

          {/* Content / role-specific fields */}
          {(atom.style === "task_aug" ||
            atom.style === "subtask" ||
            atom.style === "plan" ||
            atom.style === "memory" ||
            atom.style === "interjection") && (
            <div className="field">
              <label className="field-label" htmlFor={`${uid}-content`}>
                <T>
                  {atom.style === "subtask"
                    ? "Subtask"
                    : atom.style === "task_aug"
                      ? "Task augmentation"
                      : atom.style === "plan"
                        ? "Plan"
                        : atom.style === "memory"
                          ? "Robot memory"
                          : "Interjection"}
                </T>
              </label>
              <T>
                {atom.style === "task_aug" ||
                atom.style === "subtask" ||
                atom.style === "interjection" ? (
                  <textarea
                    id={`${uid}-content`}
                    rows={3}
                    value={atom.content || ""}
                    onChange={(e) => onChange({ content: e.target.value })}
                  />
                ) : (
                  <textarea
                    id={`${uid}-content`}
                    rows={4}
                    value={atom.content || ""}
                    onChange={(e) => onChange({ content: e.target.value })}
                  />
                )}
              </T>
            </div>
          )}

          {isSpeech && atom.tool_calls && (
            <div className="field">
              <label className="field-label" htmlFor={`${uid}-speech`}>
                <T>Robot speech (say tool call)</T>
              </label>
              <input
                id={`${uid}-speech`}
                type="text"
                value={speechText(atom) || ""}
                onChange={(e) => {
                  const next = atom.tool_calls
                    ? atom.tool_calls.map((tc, i) =>
                        i === 0
                          ? {
                              ...tc,
                              function: {
                                ...tc.function,
                                arguments: { text: e.target.value },
                              },
                            }
                          : tc,
                      )
                    : null;
                  onChange({ tool_calls: next });
                }}
              />
            </div>
          )}

          {atom.style === "vqa" && (
            <>
              <CameraField
                atom={atom}
                cameraKeys={cameraKeys}
                onChange={onChange}
              />
              <VqaEditorFields atom={atom} onChange={onChange} />
            </>
          )}
        </div>
      }
    </T>
  );
};

// ---------------------------------------------------------------------------
// CameraField — surface the row-level camera tag for VQA atoms (PR 3467).
// ---------------------------------------------------------------------------

const CameraField: React.FC<{
  atom: LanguageAtom;
  cameraKeys: string[];
  onChange: (updates: Partial<LanguageAtom>) => void;
}> = ({ atom, cameraKeys, onChange }) => {
  const uid = useId();
  if (atom.style !== "vqa") return null;
  if (cameraKeys.length === 0) return null;
  const value = atom.camera ?? "";
  return (
    <T>
      {
        <div className="field">
          <label className="field-label" htmlFor={`${uid}-camera`}>
            <T>Camera</T>
          </label>
          <select
            id={`${uid}-camera`}
            value={value}
            onChange={(e) =>
              onChange({
                camera: e.target.value === "" ? null : e.target.value,
              })
            }
          >
            <option value="">
              <T>(any — renders on every camera)</T>
            </option>
            {cameraKeys.map((k) => (
              <option key={k} value={k}>
                <T>{k}</T>
              </option>
            ))}
          </select>
        </div>
      }
    </T>
  );
};

const VqaEditorFields: React.FC<{
  atom: LanguageAtom;
  onChange: (updates: Partial<LanguageAtom>) => void;
}> = ({ atom, onChange }) => {
  const uid = useId();
  const parsed = parseVqaAnswer(atom.content);
  const kind = parsed ? classifyVqa(parsed) : null;

  if (atom.role === "user") {
    return (
      <T>
        {
          <div className="field">
            <label className="field-label" htmlFor={`${uid}-question`}>
              <T>Question</T>
            </label>
            <input
              id={`${uid}-question`}
              type="text"
              value={atom.content || ""}
              onChange={(e) => onChange({ content: e.target.value })}
            />
          </div>
        }
      </T>
    );
  }

  // Assistant atom — answer JSON (raw + structured viewer)
  return (
    <T>
      {
        <div className="field">
          <label className="field-label" htmlFor={`${uid}-answer`}>
            <T>Answer (</T>
            {kind || "unknown"})
          </label>
          <textarea
            id={`${uid}-answer`}
            rows={5}
            style={{
              fontFamily:
                "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            }}
            value={atom.content || ""}
            onChange={(e) => onChange({ content: e.target.value })}
          />
          {parsed && kind === "bbox" && (
            <p className="text-xs text-(--ds-text-secondary) mt-1">
              <T>
                Tip: bbox values are 0..1 image-relative (xyxy). Edit on the
                video itself by deleting this and re-drawing.
              </T>
            </p>
          )}
          {parsed && kind === "keypoint" && (
            <p className="text-xs text-(--ds-text-secondary) mt-1">
              <T>Tip: point values are 0..1 image-relative (xy).</T>
            </p>
          )}
        </div>
      }
    </T>
  );
};
