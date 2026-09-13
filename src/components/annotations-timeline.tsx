// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

/**
 * Multi-track timeline for v3.1 language atoms — like a video-editing
 * scrubber, but stacked vertically by style and split into two banded
 * sections that mirror the two language columns:
 *
 *   PERSISTENT (language_persistent — broadcast across every frame):
 *   - task_aug: task phrasings shown as point-in-time ticks at episode start.
 *   - subtask: filled spans from each emit time until the next subtask emit
 *     (or episode end). Numbered. Resizable edges; the empty subtask track
 *     also accepts drag-to-create.
 *   - plan: filled (read-only) spans from each plan emit until the next plan
 *     refresh (or episode end) — a plan is the active state until superseded,
 *     so it reads as a span, not an instantaneous event.
 *   - memory: tick marks (state snapshots captured at subtask boundaries).
 *
 *   EVENTS (language_events — fire on a single frame):
 *   - interjections + speech: combined event track.
 *   - vqa: event track.
 *
 * Interactions:
 *   - Click a marker → seek + select (handled by the panel's listening to
 *     `selectAtom` via context).
 *   - Drag a subtask span's left edge → retime that subtask's start.
 *   - Drag a subtask span's right edge → retime the *next* subtask's start
 *     (since the right edge of subtask[i] *is* the start of subtask[i+1]).
 *   - Drag from empty area on the subtask track → create a new subtask span;
 *     a centered label popup appears so you can name it.
 *   - Drag the playhead handle (or click anywhere on the track band) → scrub
 *     the video time. Pauses the player while dragging.
 *   - Hover over any marker → custom tooltip shows the atom's content.
 */

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useTime } from "../context/time-context";
import { useAnnotations } from "../context/annotations-context";
import {
  classifyVqa,
  isSpeechAtom,
  parseVqaAnswer,
  type LanguageAtom,
  type LanguageStyle,
  type Role,
} from "../types/language.types";
import { DraggablePopup } from "./draggable-popup";

const LABEL_WIDTH = 84;
const DRAG_THRESHOLD_PX = 4;

// `render` controls how a lane draws: "span-edit" = resizable + drag-to-create
// (subtask), "span-ro" = read-only spans (task_aug / plan), "tick" = point
// markers.
const TRACK_GROUPS = [
  {
    column: "persistent",
    title: "Persistent",
    sub: "language_persistent · broadcast across every frame",
    tracks: [
      // task_aug applies to the whole episode (it's a rephrasing of the task,
      // stored at t0 but persistent across every frame), so it reads as a
      // full-episode span — matching how the annotation pipeline treats it.
      // We collapse all rephrasings into a single full-width bar with a ×N badge;
      // clicking opens a popover listing every phrasing.
      {
        key: "task_aug",
        label: "task aug",
        color: "#38bdf8",
        render: "task-aug",
      },
      {
        key: "subtask",
        label: "subtask",
        color: "#ffd21e",
        render: "span-edit",
      },
      { key: "plan", label: "plan", color: "#5b8cff", render: "span-ro" },
      { key: "memory", label: "memory", color: "#b78bff", render: "tick" },
    ],
  },
  {
    column: "events",
    title: "Events",
    sub: "language_events · fire on a single frame",
    tracks: [
      {
        key: "interjection",
        label: "speech",
        color: "#ef5350",
        render: "tick",
      },
      { key: "vqa", label: "vqa", color: "#34d399", render: "tick" },
    ],
  },
] as const;

type TrackKey = (typeof TRACK_GROUPS)[number]["tracks"][number]["key"];

/**
 * Styles the timeline can create directly via drag-to-select, with the
 * `role` each one is authored as. `vqa` is deliberately excluded: a VQA
 * atom needs a camera plus a structured bbox/keypoint/count/attribute/
 * spatial answer, which a bare text popup can't produce correctly — VQA
 * atoms are created by drawing on the video (see the "Grounded VQA" intro
 * in episode-viewer.tsx) and only reviewed/re-timed here. `task_aug` is
 * excluded too: it's a whole-episode rephrasing with no temporal freedom
 * (always `[0, duration]`), created via the quick-add form.
 */
const CREATE_ATOM_DEFAULTS: Partial<
  Record<TrackKey, { role: Role; style: LanguageStyle }>
> = {
  subtask: { role: "assistant", style: "subtask" },
  plan: { role: "assistant", style: "plan" },
  memory: { role: "assistant", style: "memory" },
  interjection: { role: "user", style: "interjection" },
};

interface Props {
  /** Episode duration in seconds. */
  duration: number;
}

interface Tooltip {
  x: number;
  y: number;
  meta: string;
  text: string;
}

interface DragState {
  /**
   * "edge" moves an atom's `timestamp`; "edge-end" moves an atom's explicit
   * `to` (only reachable once an atom has one — see `onEdgeDown`).
   */
  kind: "edge" | "edge-end" | "playhead" | "create";
  /** Atom index whose timestamp/`to` is being moved (edge / edge-end). */
  atomIdx?: number;
  /** Episode-second timestamps captured at drag start (for cancel/clamp). */
  origTs?: number;
  prevTs?: number; // lower bound
  nextTs?: number; // upper bound (exclusive)
  /** For drag-to-create only. */
  trackKey?: TrackKey;
  startTs?: number;
  endTs?: number;
}

interface PendingCreate {
  trackKey: TrackKey;
  start: number;
  end: number;
}

export const AnnotationsTimeline: React.FC<Props> = ({ duration }) => {
  const { atoms, addAtom, updateAtom, snap, selectAtom } = useAnnotations();
  const { currentTime, seek, setIsPlaying } = useTime();
  const trackBandRef = useRef<HTMLDivElement | null>(null);

  const [tooltip, setTooltip] = useState<Tooltip | null>(null);
  // Precise time readout under the cursor, updated continuously while
  // hovering any track (not just markers/spans) — helps pick exact left/right
  // range bounds by eye before committing to a drag. `null` while the mouse
  // isn't over the track area.
  const [hoverTs, setHoverTs] = useState<number | null>(null);
  const [drag, setDrag] = useState<DragState | null>(null);
  const [pendingCreate, setPendingCreate] = useState<PendingCreate | null>(
    null,
  );
  const [createLabel, setCreateLabel] = useState("");

  // Pause + select helper
  const jumpAndSelect = React.useCallback(
    (ts: number, idx: number | null) => {
      seek(ts, "external");
      setIsPlaying(false);
      if (idx != null) selectAtom(idx);
    },
    [seek, setIsPlaying, selectAtom],
  );

  // ============ Lane derivation ============
  const lanes = useMemo(() => {
    type SpanMarker = {
      kind: "span";
      start: number;
      end: number;
      label: string;
      atom: LanguageAtom;
      atomIdx: number; // index of the *start* atom in atoms[]
    };
    type TickMarker = {
      kind: "tick";
      t: number;
      label: string;
      atom: LanguageAtom;
      atomIdx: number;
      subtype?: string;
    };

    const subtask: SpanMarker[] = [];
    const task_aug: SpanMarker[] = [];
    const plan: SpanMarker[] = [];
    const memory: TickMarker[] = [];
    const interjection: TickMarker[] = [];
    const vqa: TickMarker[] = [];

    // Subtasks → spans, sorted by ts. Track the original atom index so drag
    // operations can update via updateAtom(idx, ...).
    const subWithIdx = atoms
      .map((a, i) => ({ a, i }))
      .filter(({ a }) => a.style === "subtask")
      .sort((x, y) => x.a.timestamp - y.a.timestamp);
    subWithIdx.forEach(({ a, i }, k) => {
      const start = a.timestamp;
      // An explicit `to` (drag-authored range) always wins; otherwise fall
      // back to "active until the next subtask" for every atom saved before
      // this feature existed.
      const end =
        a.to != null
          ? a.to
          : k + 1 < subWithIdx.length
            ? subWithIdx[k + 1].a.timestamp
            : duration;
      subtask.push({
        kind: "span",
        start,
        end,
        label: a.content || "",
        atom: a,
        atomIdx: i,
      });
    });

    // Plans → read-only spans: a plan is the active state from its emit time
    // until the next plan refresh (or episode end), exactly like a subtask
    // span. Rendering it as a span (not a tick) makes its persistent nature
    // visible — it isn't a point-in-time event.
    const planWithIdx = atoms
      .map((a, i) => ({ a, i }))
      .filter(({ a }) => a.style === "plan")
      .sort((x, y) => x.a.timestamp - y.a.timestamp);
    planWithIdx.forEach(({ a, i }, k) => {
      const start = a.timestamp;
      const end =
        a.to != null
          ? a.to
          : k + 1 < planWithIdx.length
            ? planWithIdx[k + 1].a.timestamp
            : duration;
      plan.push({
        kind: "span",
        start,
        end,
        label: a.content || "plan",
        atom: a,
        atomIdx: i,
      });
    });

    // Task augmentations → full-episode spans: each is a rephrasing of the
    // task and applies to the whole episode (persistent, stored at t0), so it
    // spans [t0, t_last] rather than sitting as a tick at the start.
    atoms.forEach((a, i) => {
      if (a.style === "task_aug") {
        task_aug.push({
          kind: "span",
          start: 0,
          end: duration,
          label: a.content || "task augmentation",
          atom: a,
          atomIdx: i,
        });
      }
    });

    atoms.forEach((a, i) => {
      if (a.style === "memory") {
        memory.push({
          kind: "tick",
          t: a.timestamp,
          label: a.content || "memory",
          atom: a,
          atomIdx: i,
        });
      } else if (a.style === "interjection" || isSpeechAtom(a)) {
        interjection.push({
          kind: "tick",
          t: a.timestamp,
          label: a.style === "interjection" ? a.content || "" : "say(…)",
          atom: a,
          atomIdx: i,
          subtype: a.style === "interjection" ? "user" : "speech",
        });
      } else if (a.style === "vqa" && a.role === "assistant") {
        const parsed = parseVqaAnswer(a.content);
        const kind = parsed ? classifyVqa(parsed) : null;
        vqa.push({
          kind: "tick",
          t: a.timestamp,
          label: kind || "vqa",
          atom: a,
          atomIdx: i,
          subtype: kind || undefined,
        });
      }
    });

    return { task_aug, subtask, plan, memory, interjection, vqa, subWithIdx };
  }, [atoms, duration]);

  // ============ Pixel <-> time mapping ============
  // The full-width track band (no label margin) is `trackBandRef`. Convert
  // mouse client.x to a 0..duration timestamp.
  const trackXToTs = useCallback(
    (clientX: number): number => {
      const r = trackBandRef.current?.getBoundingClientRect();
      if (!r || !duration) return 0;
      const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      return frac * duration;
    },
    [duration],
  );

  // ============ Continuous hover time readout ============
  // Attached to every track div (not just markers) so moving the mouse
  // anywhere over any lane always shows the precise underlying time.
  const onTrackHoverMove = (e: React.MouseEvent) => {
    setHoverTs(trackXToTs(e.clientX));
  };
  const onTrackHoverLeave = () => setHoverTs(null);

  // ============ Event-track click → seek + select ============
  const onTickClick = (e: React.MouseEvent, atomIdx: number, t: number) => {
    e.stopPropagation();
    jumpAndSelect(t, atomIdx);
  };

  // ============ task_aug collapsed-bar click ============
  // All phrasings share t0, so there is no spatial way to disambiguate them
  // on the track — clicking just selects the first one (the full list is
  // shown on hover). The inspector + rail still expose every rewording.
  const onTaskAugClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    const augs = lanes.task_aug;
    if (augs.length === 0) return;
    jumpAndSelect(0, augs[0].atomIdx);
  };

  // ============ Subtask span drag ============
  const onSpanBodyClick = (
    e: React.MouseEvent,
    atomIdx: number,
    start: number,
  ) => {
    if (drag || pendingCreate) return;
    e.stopPropagation();
    jumpAndSelect(start, atomIdx);
  };

  const onEdgeDown = (
    e: React.PointerEvent,
    side: "l" | "r",
    spanK: number,
  ) => {
    e.stopPropagation();
    e.preventDefault();
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    // The drag now seeks the video to follow the edge (see the pointermove
    // handler) — pause first so that doesn't fight ongoing playback.
    setIsPlaying(false);
    const sub = lanes.subWithIdx;
    // Right edge of a span that owns an explicit `to` (drag-authored range):
    // resize THIS atom's `to`, never touch the neighbor.
    if (side === "r" && sub[spanK]?.a.to != null) {
      const self = sub[spanK];
      const lower = self.a.timestamp;
      const upper =
        spanK + 1 < sub.length ? sub[spanK + 1].a.timestamp : duration;
      setDrag({
        kind: "edge-end",
        atomIdx: self.i,
        origTs: self.a.to ?? undefined,
        prevTs: lower,
        nextTs: upper,
      });
      return;
    }
    // Left edge of span k → moves sub[k] timestamp.
    // Right edge of span k with no explicit `to` (legacy) → moves sub[k+1]
    // timestamp, since that neighbor's start *is* this span's implied end.
    const idxToMove = side === "l" ? spanK : spanK + 1;
    if (idxToMove < 0 || idxToMove >= sub.length) return;
    const target = sub[idxToMove];
    const lower = idxToMove > 0 ? sub[idxToMove - 1].a.timestamp : 0;
    const rawUpper =
      idxToMove + 1 < sub.length ? sub[idxToMove + 1].a.timestamp : duration;
    // Moving this atom's own start can never cross its own explicit end.
    const upper =
      side === "l" && target.a.to != null
        ? Math.min(rawUpper, target.a.to)
        : rawUpper;
    setDrag({
      kind: "edge",
      atomIdx: target.i,
      origTs: target.a.timestamp,
      prevTs: lower,
      nextTs: upper,
    });
  };

  // ============ Drag-to-create a new atom on any creatable track ============
  const onTrackDown = (e: React.PointerEvent, trackKey: TrackKey) => {
    // Only fire when the mousedown lands on the track itself, not on a
    // child span/edge (those stop propagation in their own handlers).
    if (drag || pendingCreate) return;
    if (e.button !== 0) return;
    if (!(trackKey in CREATE_ATOM_DEFAULTS)) return;
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    // The drag now seeks the video to follow the range's end (see the
    // pointermove handler) — pause first so that doesn't fight ongoing
    // playback.
    setIsPlaying(false);
    const ts = snap(trackXToTs(e.clientX));
    setDrag({ kind: "create", trackKey, startTs: ts, endTs: ts });
  };

  // ============ Playhead drag ============
  const onPlayheadDown = (e: React.PointerEvent) => {
    e.stopPropagation();
    e.preventDefault();
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    setIsPlaying(false);
    setDrag({ kind: "playhead" });
  };

  const onTrackBandClick = (e: React.MouseEvent) => {
    // Clicks anywhere on the track band that bubbled up: seek to that point.
    if (drag || pendingCreate) return;
    if ((e.target as HTMLElement).dataset.role === "ruler") {
      // Already handled by the dedicated ruler bar
    }
    const ts = trackXToTs(e.clientX);
    seek(ts, "external");
    setIsPlaying(false);
  };

  // ============ Global pointermove / pointerup for drag commits ============
  useEffect(() => {
    if (!drag) return;
    const move = (e: PointerEvent) => {
      const ts = trackXToTs(e.clientX);
      if (drag.kind === "playhead") {
        seek(Math.max(0, Math.min(duration, ts)), "external");
        return;
      }
      if (drag.kind === "edge" && drag.atomIdx != null) {
        const lower = drag.prevTs ?? 0;
        const upper = drag.nextTs ?? duration;
        const clamped = Math.max(lower + 0.001, Math.min(upper - 0.001, ts));
        const snapped = snap(clamped);
        updateAtom(drag.atomIdx, { timestamp: snapped });
        // Follow the edge being dragged with the playhead/video so the user
        // can see the frame they're landing the boundary on, same as
        // dragging the playhead handle itself.
        seek(snapped, "external");
        return;
      }
      if (drag.kind === "edge-end" && drag.atomIdx != null) {
        const lower = drag.prevTs ?? 0;
        const upper = drag.nextTs ?? duration;
        const clamped = Math.max(lower + 0.001, Math.min(upper - 0.001, ts));
        const snapped = snap(clamped);
        updateAtom(drag.atomIdx, { to: snapped });
        seek(snapped, "external");
        return;
      }
      if (drag.kind === "create") {
        const snapped = snap(Math.max(0, Math.min(duration, ts)));
        setDrag((d) => (d ? { ...d, endTs: snapped } : d));
        // Follow the end of the range being drawn — lets the user watch the
        // video land on whichever frame they're currently dragging over,
        // same reasoning as the edge-drag case above.
        seek(snapped, "external");
      }
    };
    const up = (e: PointerEvent) => {
      if (drag.kind === "create" && drag.trackKey) {
        // Read the release position straight from the event rather than
        // `drag.endTs` — `move`'s setDrag is async, so on a very fast
        // drag-and-release the `up` closure can still be holding the
        // *previous* render's stale `endTs` (equal to `startTs`) when this
        // fires, which would wrongly fall through to the tap/seek branch
        // below instead of opening the create-label popup.
        const liveEndTs = snap(
          Math.max(0, Math.min(duration, trackXToTs(e.clientX))),
        );
        const a = Math.min(drag.startTs ?? 0, liveEndTs);
        const b = Math.max(drag.startTs ?? 0, liveEndTs);
        const distFrac = Math.abs(b - a) / Math.max(0.001, duration);
        // Need at least a few px of drag to count, otherwise treat as click.
        const trackWidth =
          trackBandRef.current?.getBoundingClientRect().width ?? 1;
        if (distFrac * trackWidth >= DRAG_THRESHOLD_PX) {
          // The label popup opens in the viewport centre so the input stays
          // reachable even when the range ends at a track edge.
          setPendingCreate({ trackKey: drag.trackKey, start: a, end: b });
        } else {
          // Tap, not drag — treat as a seek to that point.
          seek(a, "external");
          setIsPlaying(false);
        }
      } else if (drag.kind === "edge" && drag.atomIdx != null) {
        // Already updated in `move`; nothing more to do beyond final snap.
      }
      setDrag(null);
      // We don't release pointerCapture here because the original target is
      // already cleaned up by the browser when we release the pointer.
      void e;
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [drag, duration, seek, setIsPlaying, snap, trackXToTs, updateAtom]);

  // ============ Tooltip helpers ============
  const showTip = (e: React.MouseEvent, meta: string, text: string) => {
    setTooltip({
      x: e.clientX + 12,
      y: e.clientY + 12,
      meta,
      text,
    });
  };
  const moveTip = (e: React.MouseEvent) => {
    setTooltip((t) => (t ? { ...t, x: e.clientX + 12, y: e.clientY + 12 } : t));
  };
  const hideTip = () => setTooltip(null);

  // ============ Pending-create label popup commit ============
  const commitPendingCreate = () => {
    if (!pendingCreate) return;
    const text = createLabel.trim();
    const defaults = CREATE_ATOM_DEFAULTS[pendingCreate.trackKey];
    if (!text || !defaults) {
      setPendingCreate(null);
      setCreateLabel("");
      return;
    }
    // Reaching the popup already required a real drag (see the `up` handler
    // above — a tap seeks immediately and never opens this), so start/end
    // always differ: record the authored range explicitly.
    addAtom({
      role: defaults.role,
      content: text,
      style: defaults.style,
      timestamp: snap(pendingCreate.start),
      to: snap(pendingCreate.end),
      camera: null,
      tool_calls: null,
    });
    setPendingCreate(null);
    setCreateLabel("");
  };
  const cancelPendingCreate = () => {
    setPendingCreate(null);
    setCreateLabel("");
  };

  // ============ Render ============
  if (!duration) return null;

  return (
    <T>
      {
        <div className="tl">
          <div className="tl-head">
            <span>
              <T>Annotations timeline</T>
            </span>
            <span className="ts-display">
              {currentTime.toFixed(2)}s / {duration.toFixed(2)}s
            </span>
          </div>

          {/* Time-axis ruler — clicking it scrubs */}
          <div
            className="tl-ruler"
            data-role="ruler"
            onClick={(e) => {
              const ts = trackXToTs(e.clientX);
              seek(ts, "external");
              setIsPlaying(false);
            }}
          >
            {Array.from({ length: Math.floor(duration / 5) + 1 }).map(
              (_, i) => {
                const t = i * 5;
                const left = (t / duration) * 100;
                return (
                  <div
                    key={i}
                    className="tick-mark"
                    style={{ left: `${left}%` }}
                  >
                    <T>{t}</T>s
                  </div>
                );
              },
            )}
          </div>

          {/* Tracks, grouped into Persistent / Events sections that mirror the
           two language columns. The whole region is position:relative so the
           playhead can span its full height via top/bottom (no brittle
           per-track pixel math that section headers would throw off). The
           playhead's x uses calc() to start at the track band's left edge
           (after the LABEL_WIDTH label column + 10px gap). */}
          {(() => {
            const bandLeft = `${LABEL_WIDTH + 10}px`;
            const playheadLeft = `calc(${bandLeft} + ${
              duration ? currentTime / duration : 0
            } * (100% - ${bandLeft}))`;
            return (
              <div className="tl-tracks" style={{ position: "relative" }}>
                {TRACK_GROUPS.map((group) => (
                  <div className="tl-section" key={group.column}>
                    <div className={`tl-section-head ${group.column}`}>
                      <span className="tl-section-title">
                        <T>{group.title}</T>
                      </span>
                      <span className="tl-section-sub">
                        <T>{group.sub}</T>
                      </span>
                    </div>
                    {group.tracks.map((tk) => (
                      <div className="tl-row" key={tk.key}>
                        <div className="label">
                          <span className={`style-dot dot-${tk.key}`} />
                          <T>{tk.label}</T>
                        </div>
                        <div
                          className={`track ${
                            (drag?.kind === "create" &&
                              drag.trackKey === tk.key) ||
                            pendingCreate?.trackKey === tk.key
                              ? "creating"
                              : ""
                          }`}
                          ref={tk.key === "subtask" ? trackBandRef : undefined}
                          onClick={
                            tk.render === "span-edit"
                              ? undefined
                              : onTrackBandClick
                          }
                          onPointerDown={
                            tk.key in CREATE_ATOM_DEFAULTS
                              ? (e) => onTrackDown(e, tk.key)
                              : undefined
                          }
                          onMouseMove={onTrackHoverMove}
                          onMouseLeave={onTrackHoverLeave}
                        >
                          {/* Editable subtask spans (resize + drag-to-create) */}
                          {tk.render === "span-edit" &&
                            lanes.subtask.map((s, k) => {
                              const left = (s.start / duration) * 100;
                              const width = Math.max(
                                0.3,
                                ((s.end - s.start) / duration) * 100,
                              );
                              return (
                                <div
                                  key={k}
                                  className={`tl-seg subtask ${(drag?.kind === "edge" || drag?.kind === "edge-end") && drag.atomIdx === s.atomIdx ? "dragging" : ""}`}
                                  style={{
                                    left: `${left}%`,
                                    width: `${width}%`,
                                  }}
                                  onClick={(e) =>
                                    onSpanBodyClick(e, s.atomIdx, s.start)
                                  }
                                  onMouseEnter={(e) =>
                                    showTip(
                                      e,
                                      `subtask · ${s.start.toFixed(2)}s → ${s.end.toFixed(2)}s`,
                                      s.label,
                                    )
                                  }
                                  onMouseMove={moveTip}
                                  onMouseLeave={hideTip}
                                >
                                  <span style={{ opacity: 0.7, fontSize: 10 }}>
                                    <T>{k}</T>
                                  </span>
                                  <span
                                    style={{
                                      whiteSpace: "nowrap",
                                      overflow: "hidden",
                                      textOverflow: "ellipsis",
                                    }}
                                  >
                                    <T>{s.label}</T>
                                  </span>
                                  <div
                                    className="resize l"
                                    onPointerDown={(e) => onEdgeDown(e, "l", k)}
                                  />
                                  {/* Right handle: legacy adjacency-resize
                                  needs a next span to move; a span with its
                                  own explicit `to` can always resize itself,
                                  even as the last (or only) span. */}
                                  {(s.atom.to != null ||
                                    k + 1 < lanes.subtask.length) && (
                                    <div
                                      className="resize r"
                                      onPointerDown={(e) =>
                                        onEdgeDown(e, "r", k)
                                      }
                                    />
                                  )}
                                </div>
                              );
                            })}

                          {/* Drag-to-create preview rectangle, on whichever
                          track the drag started on. Stays visible through
                          the label popup too (pendingCreate) — without this,
                          releasing the mouse made the whole range vanish
                          from the timeline right when the popup asking you
                          to name it appeared, which is exactly the moment
                          you most want to still see it. */}
                          {(() => {
                            const preview =
                              drag?.kind === "create" &&
                              drag.trackKey === tk.key
                                ? {
                                    start: Math.min(
                                      drag.startTs ?? 0,
                                      drag.endTs ?? 0,
                                    ),
                                    end: Math.max(
                                      drag.startTs ?? 0,
                                      drag.endTs ?? 0,
                                    ),
                                  }
                                : pendingCreate?.trackKey === tk.key
                                  ? {
                                      start: pendingCreate.start,
                                      end: pendingCreate.end,
                                    }
                                  : null;
                            if (!preview) return null;
                            return (
                              <div
                                className="tl-create-preview"
                                style={{
                                  left: `${(preview.start / duration) * 100}%`,
                                  width: `${((preview.end - preview.start) / duration) * 100}%`,
                                }}
                              />
                            );
                          })()}

                          {/* Collapsed task-augmentation bar: one full-width bar
                          (rephrasings carry no temporal info), with a ×N badge
                          when there is more than one. Click selects the single
                          phrasing, or opens the rewordings popover. */}
                          {tk.render === "task-aug" &&
                            lanes.task_aug.length > 0 &&
                            (() => {
                              const augs = lanes.task_aug;
                              const primary = augs[0];
                              const count = augs.length;
                              return (
                                <div
                                  className="tl-seg task_aug"
                                  style={{ left: "0%", width: "100%" }}
                                  onClick={onTaskAugClick}
                                  onMouseEnter={(e) =>
                                    showTip(
                                      e,
                                      `task aug · ${count} phrasing${count > 1 ? "s" : ""}`,
                                      count > 1
                                        ? augs
                                            .map((s) => `• ${s.label}`)
                                            .join("\n")
                                        : primary.label,
                                    )
                                  }
                                  onMouseMove={moveTip}
                                  onMouseLeave={hideTip}
                                >
                                  <span
                                    style={{
                                      whiteSpace: "nowrap",
                                      overflow: "hidden",
                                      textOverflow: "ellipsis",
                                    }}
                                  >
                                    <T>{primary.label}</T>
                                  </span>
                                  {count > 1 && (
                                    <span className="aug-count">
                                      ×<T>{count}</T>
                                    </span>
                                  )}
                                </div>
                              );
                            })()}

                          {/* Read-only persistent spans (plan is active until its
                          next refresh). Click seeks + selects; no resize. */}
                          {tk.render === "span-ro" &&
                            (
                              lanes[tk.key as "plan"] as Array<{
                                kind: "span";
                                start: number;
                                end: number;
                                label: string;
                                atom: LanguageAtom;
                                atomIdx: number;
                              }>
                            ).map((s, k) => {
                              const left = (s.start / duration) * 100;
                              const width = Math.max(
                                0.3,
                                ((s.end - s.start) / duration) * 100,
                              );
                              return (
                                <div
                                  key={k}
                                  className={`tl-seg ${tk.key}`}
                                  style={{
                                    left: `${left}%`,
                                    width: `${width}%`,
                                  }}
                                  onClick={(e) =>
                                    onSpanBodyClick(e, s.atomIdx, s.start)
                                  }
                                  onMouseEnter={(e) =>
                                    showTip(
                                      e,
                                      `${tk.label} · ${s.start.toFixed(2)}s → ${s.end.toFixed(2)}s`,
                                      s.label,
                                    )
                                  }
                                  onMouseMove={moveTip}
                                  onMouseLeave={hideTip}
                                >
                                  <span
                                    style={{
                                      whiteSpace: "nowrap",
                                      overflow: "hidden",
                                      textOverflow: "ellipsis",
                                    }}
                                  >
                                    <T>{s.label}</T>
                                  </span>
                                </div>
                              );
                            })}

                          {/* Point-in-time tick markers (task_aug / memory /
                          interjection / vqa) */}
                          {tk.render === "tick" &&
                            (
                              lanes[
                                tk.key as "memory" | "interjection" | "vqa"
                              ] as Array<{
                                kind: "tick";
                                t: number;
                                label: string;
                                atom: LanguageAtom;
                                atomIdx: number;
                                subtype?: string;
                              }>
                            ).map((m, i) => {
                              const left = (m.t / duration) * 100;
                              const to = m.atom.to;
                              const hasRange = to != null && to > m.t;
                              return (
                                <React.Fragment key={i}>
                                  {hasRange && (
                                    <div
                                      className={`tl-seg tick-range ${tk.key}`}
                                      style={{
                                        left: `${left}%`,
                                        width: `${Math.max(0.3, ((to - m.t) / duration) * 100)}%`,
                                      }}
                                      onClick={(e) =>
                                        onTickClick(e, m.atomIdx, m.t)
                                      }
                                    />
                                  )}
                                  <div
                                    className={`tl-tick ${tk.key}`}
                                    style={{ left: `${left}%` }}
                                    onClick={(e) =>
                                      onTickClick(e, m.atomIdx, m.t)
                                    }
                                    onMouseEnter={(e) =>
                                      showTip(
                                        e,
                                        `${tk.label}${m.subtype ? ` · ${m.subtype}` : ""} · ${m.t.toFixed(3)}s${hasRange ? ` → ${to.toFixed(3)}s` : ""}`,
                                        m.label,
                                      )
                                    }
                                    onMouseMove={moveTip}
                                    onMouseLeave={hideTip}
                                  />
                                </React.Fragment>
                              );
                            })}
                        </div>
                      </div>
                    ))}
                  </div>
                ))}

                {/* Playhead — spans the full tracks region via top/bottom. */}
                <div className="tl-playhead" style={{ left: playheadLeft }} />
                <div
                  className="tl-playhead-handle"
                  style={{ left: playheadLeft, top: -6 }}
                  onPointerDown={onPlayheadDown}
                  title="Drag to scrub"
                />

                {/* Continuous hover-time readout — shown for any mouse
                position over any track, including while dragging (the drag
                already keeps the video/playhead synced to this same value;
                this is the precise decimal-seconds number to go with it). */}
                {hoverTs != null &&
                  (() => {
                    const hoverLeft = `calc(${bandLeft} + ${
                      duration ? hoverTs / duration : 0
                    } * (100% - ${bandLeft}))`;
                    return (
                      <>
                        <div
                          className="tl-hover-line"
                          style={{ left: hoverLeft }}
                        />
                        <div
                          className="tl-hover-time"
                          style={{ left: hoverLeft }}
                        >
                          {hoverTs.toFixed(3)}s
                        </div>
                      </>
                    );
                  })()}
              </div>
            );
          })()}

          {/* Tooltip */}
          {tooltip && (
            <div
              className="tl-tooltip"
              style={{ left: tooltip.x, top: tooltip.y }}
            >
              <div className="meta">
                <T>{tooltip.meta}</T>
              </div>
              <T>{tooltip.text}</T>
            </div>
          )}

          {/* Drag-to-create label popup */}
          {pendingCreate && (
            <DraggablePopup
              header={
                <>
                  <span className={`style-pill ${pendingCreate.trackKey}`}>
                    <T>{pendingCreate.trackKey}</T>
                  </span>
                  <span style={{ marginLeft: "auto", fontFamily: "monospace" }}>
                    {pendingCreate.start.toFixed(2)}s →{" "}
                    {pendingCreate.end.toFixed(2)}s
                  </span>
                </>
              }
              ariaLabel="Create annotation"
              canSubmit={createLabel.trim().length > 0}
              onSubmit={commitPendingCreate}
              onCancel={cancelPendingCreate}
            >
              <input
                type="text"
                placeholder="label (e.g. grasp the sponge)"
                autoFocus
                value={createLabel}
                onChange={(e) => setCreateLabel(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") commitPendingCreate();
                }}
              />
              <div className="quick-popup-actions">
                <button
                  onClick={cancelPendingCreate}
                  style={{
                    fontSize: 11,
                    padding: "4px 8px",
                    borderRadius: 6,
                    border: "1px solid rgba(255,255,255,0.12)",
                    background: "transparent",
                    color: "var(--fg-2, #cbd5e1)",
                    cursor: "pointer",
                  }}
                >
                  <T>cancel</T>
                </button>
                <button
                  onClick={commitPendingCreate}
                  disabled={!createLabel.trim()}
                  style={{
                    fontSize: 11,
                    padding: "4px 8px",
                    borderRadius: 6,
                    border: "1px solid #5b8cff",
                    background: "rgba(91,140,255,0.15)",
                    color: "#c7d6ff",
                    cursor: "pointer",
                    opacity: createLabel.trim() ? 1 : 0.4,
                  }}
                >
                  <T>add ↵</T>
                </button>
              </div>
            </DraggablePopup>
          )}
        </div>
      }
    </T>
  );
};
