// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";

/**
 * Canvas overlay rendered on top of a single `<video>` element. Three roles:
 *
 * 1. Display VQA bbox/keypoint atoms whose `timestamp` matches the current
 *    video time (within ~one frame) and whose optional `camera` field matches
 *    this video's camera key (or has no camera, which we treat as
 *    "render on every camera").
 *
 * 2. Display reviewed SAM3 object masks and track bboxes at the current
 *    episode-local frame.
 *
 * 3. When the user is in "draw mode" — bbox or keypoint — capture mouse input
 *    and stage a `pendingDraw` in the AnnotationsContext so the AnnotationsPanel
 *    can pick it up and persist it as a VQA atom.
 *
 * Coordinates are stored in 0..1 image-relative space. Drawing is computed
 * against the actually-rendered video rect (i.e. taking `object-contain`
 * letterboxing into account).
 */

import React, { useEffect, useRef, useState } from "react";
import { DraggablePopup } from "./draggable-popup";
import {
  useAnnotations,
  type PendingBboxDraw,
  type PendingPointDraw,
} from "../context/annotations-context";
import { useTime } from "../context/time-context";
import {
  classifyVqa,
  parseVqaAnswer,
  type LanguageAtom,
  type VqaAnswer,
} from "../types/language.types";
import type { ObjectAnnotation } from "../types/object-annotation.types";

interface Props {
  videoEl: HTMLVideoElement | null;
  cameraKey: string;
  objectAnnotations?: ObjectAnnotation[];
}

interface RenderedRect {
  // Position of the video's actual rendered image area inside the canvas
  // (which is positioned to fill the video's bounding box). The video uses
  // `object-contain` so for a video aspect mismatched with its container,
  // there's letterboxing — we need to compute the inner rect to map 0..1
  // image-relative coordinates correctly.
  left: number;
  top: number;
  width: number;
  height: number;
  // Source image dimensions in pixels — needed so we can also map
  // pixel-space VQA answers (the annotation pipeline emits bboxes in
  // ``[x_min, y_min, x_max, y_max]`` source pixels per Module 3's prompt).
  sourceWidth: number;
  sourceHeight: number;
}

function computeRenderedRect(
  canvas: HTMLCanvasElement,
  video: HTMLVideoElement,
): RenderedRect {
  // Use CSS dimensions, not bitmap dimensions — the canvas is HiDPI-scaled
  // (canvas.width = cssWidth * dpr) and the 2D context already has a
  // transform applied, so all drawing happens in CSS-pixel space.
  const cssW = canvas.clientWidth || canvas.width;
  const cssH = canvas.clientHeight || canvas.height;
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) {
    // Metadata not yet loaded — fall back to the full canvas. The
    // `loadedmetadata` listener forces a redraw the moment vw/vh are known
    // so this fallback is short-lived.
    return {
      left: 0,
      top: 0,
      width: cssW,
      height: cssH,
      sourceWidth: 0,
      sourceHeight: 0,
    };
  }
  const videoAspect = vw / vh;
  const containerAspect = cssW / cssH;
  if (containerAspect > videoAspect) {
    // Container is wider than the video → vertical fill, horizontal letterbox.
    const renderedW = cssH * videoAspect;
    return {
      left: (cssW - renderedW) / 2,
      top: 0,
      width: renderedW,
      height: cssH,
      sourceWidth: vw,
      sourceHeight: vh,
    };
  } else {
    const renderedH = cssW / videoAspect;
    return {
      left: 0,
      top: (cssH - renderedH) / 2,
      width: cssW,
      height: renderedH,
      sourceWidth: vw,
      sourceHeight: vh,
    };
  }
}

const GROUNDING_COORDINATE_SCALE = 1000;

function isUnitCoord(x: number, y: number): boolean {
  return Math.max(Math.abs(x), Math.abs(y)) <= 1.5;
}

function isGroundingCoord(x: number, y: number): boolean {
  return (
    Math.max(Math.abs(x), Math.abs(y)) <= GROUNDING_COORDINATE_SCALE &&
    Math.max(Math.abs(x), Math.abs(y)) > 1.5
  );
}

function mapPointToCanvas(
  rect: RenderedRect,
  x: number,
  y: number,
): [number, number] {
  if (isUnitCoord(x, y)) {
    return [rect.left + x * rect.width, rect.top + y * rect.height];
  }

  // Model-generated grounding annotations commonly use a 0..1000 image grid.
  // The videos are often lower resolution, so treating those values as source
  // pixels makes boxes/points too large after rendering.
  const scaleX = isGroundingCoord(x, y)
    ? GROUNDING_COORDINATE_SCALE
    : rect.sourceWidth || rect.width;
  const scaleY = isGroundingCoord(x, y)
    ? GROUNDING_COORDINATE_SCALE
    : rect.sourceHeight || rect.height;

  return [
    rect.left + (x / scaleX) * rect.width,
    rect.top + (y / scaleY) * rect.height,
  ];
}

function drawBbox(
  ctx: CanvasRenderingContext2D,
  rect: RenderedRect,
  bbox: [number, number, number, number],
  bboxFormat: string,
  label: string,
  color: string,
) {
  const [bx1, by1, bx2, by2] = bbox;
  const x1 = bx1;
  const y1 = by1;
  let x2 = bx2;
  let y2 = by2;
  if (bboxFormat === "xywh") {
    x2 = x1 + bx2;
    y2 = y1 + by2;
  }
  const [px1, py1] = mapPointToCanvas(rect, x1, y1);
  const [px2, py2] = mapPointToCanvas(rect, x2, y2);
  ctx.lineWidth = 2;
  ctx.strokeStyle = color;
  ctx.fillStyle = color + "26"; // ~15% alpha
  ctx.fillRect(px1, py1, px2 - px1, py2 - py1);
  ctx.strokeRect(px1, py1, px2 - px1, py2 - py1);
  if (label) {
    ctx.font = "12px ui-sans-serif, system-ui";
    const m = ctx.measureText(label);
    ctx.fillStyle = "#0b0e14";
    ctx.fillRect(px1, py1 - 16, m.width + 8, 16);
    ctx.fillStyle = color;
    ctx.fillText(label, px1 + 4, py1 - 4);
  }
}

function drawPoint(
  ctx: CanvasRenderingContext2D,
  rect: RenderedRect,
  point: [number, number],
  label: string,
  color: string,
) {
  const [x, y] = point;
  const [px, py] = mapPointToCanvas(rect, x, y);
  ctx.lineWidth = 2;
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(px, py, 5, 0, Math.PI * 2);
  ctx.fill();
  ctx.beginPath();
  ctx.arc(px, py, 11, 0, Math.PI * 2);
  ctx.stroke();
  if (label) {
    ctx.font = "12px ui-sans-serif, system-ui";
    const m = ctx.measureText(label);
    ctx.fillStyle = "#0b0e14";
    ctx.fillRect(px + 8, py - 18, m.width + 8, 16);
    ctx.fillStyle = color;
    ctx.fillText(label, px + 12, py - 6);
  }
}

function vqaMatchesCamera(answer: VqaAnswer, cameraKey: string): boolean {
  // If the answer doesn't carry a camera field, render it on every camera.
  // If it does, only render where it matches.
  const kind = classifyVqa(answer);
  if (kind === "bbox") {
    const dets = (answer as { detections: Array<{ camera?: string }> })
      .detections;
    if (!dets.length) return false;
    return dets.some((d) => !d.camera || d.camera === cameraKey);
  }
  if (kind === "keypoint") {
    const c = (answer as { camera?: string }).camera;
    return !c || c === cameraKey;
  }
  return false; // other VQA kinds aren't drawn
}

const OBJECT_COLORS = [
  "#22d3ee",
  "#a78bfa",
  "#34d399",
  "#fbbf24",
  "#fb7185",
  "#60a5fa",
];

function objectColor(trackId: number): string {
  return OBJECT_COLORS[Math.abs(trackId) % OBJECT_COLORS.length];
}

function drawObjectMask(
  ctx: CanvasRenderingContext2D,
  rect: RenderedRect,
  annotation: ObjectAnnotation,
  cache: Map<string, HTMLCanvasElement>,
  color: string,
) {
  const key = `${annotation.object_id}:${annotation.frame_index}`;
  let maskCanvas = cache.get(key);
  if (!maskCanvas) {
    const [height, width] = annotation.mask_rle.size;
    if (!height || !width || annotation.mask_rle.counts.length === 0) return;
    maskCanvas = document.createElement("canvas");
    maskCanvas.width = width;
    maskCanvas.height = height;
    const maskContext = maskCanvas.getContext("2d");
    if (!maskContext) return;
    const image = maskContext.createImageData(width, height);
    let cursor = 0;
    let foreground = false;
    for (const count of annotation.mask_rle.counts) {
      if (foreground) {
        for (let offset = 0; offset < count; offset += 1) {
          const linear = cursor + offset;
          const y = linear % height;
          const x = Math.floor(linear / height);
          if (x >= width) continue;
          const pixel = (y * width + x) * 4;
          // Use a translucent fill so the underlying RGB frame remains
          // readable while the mask boundary and label stay prominent.
          const hex = color.slice(1);
          image.data[pixel] = Number.parseInt(hex.slice(0, 2), 16);
          image.data[pixel + 1] = Number.parseInt(hex.slice(2, 4), 16);
          image.data[pixel + 2] = Number.parseInt(hex.slice(4, 6), 16);
          image.data[pixel + 3] = 92;
        }
      }
      cursor += count;
      foreground = !foreground;
    }
    maskContext.putImageData(image, 0, 0);
    cache.set(key, maskCanvas);
  }
  ctx.drawImage(maskCanvas, rect.left, rect.top, rect.width, rect.height);
}

function drawObjectBbox(
  ctx: CanvasRenderingContext2D,
  rect: RenderedRect,
  annotation: ObjectAnnotation,
  color: string,
) {
  const [height, width] = annotation.image_size;
  if (!height || !width) return;
  const [x1, y1, x2, y2] = annotation.bbox_xyxy;
  const px1 = rect.left + (x1 / width) * rect.width;
  const py1 = rect.top + (y1 / height) * rect.height;
  const px2 = rect.left + (x2 / width) * rect.width;
  const py2 = rect.top + (y2 / height) * rect.height;
  ctx.save();
  ctx.lineWidth = annotation.status === "suggested" ? 2 : 2.5;
  ctx.setLineDash(annotation.status === "needs_review" ? [6, 4] : []);
  ctx.strokeStyle = color;
  ctx.strokeRect(px1, py1, px2 - px1, py2 - py1);
  const label = `#${annotation.track_id} ${annotation.concept}`;
  ctx.font = "12px ui-sans-serif, system-ui";
  const metrics = ctx.measureText(label);
  const labelTop = Math.max(0, py1 - 18);
  ctx.fillStyle = "#0b0e14e6";
  ctx.fillRect(px1, labelTop, metrics.width + 8, 18);
  ctx.fillStyle = color;
  ctx.fillText(label, px1 + 4, labelTop + 13);
  ctx.restore();
}

/** Pixel distance below which a pointer up counts as a click, not a drag. */
const CLICK_THRESHOLD_PX = 4;

interface FinalizingState {
  /** What the user just drew (label/camera filled in on submit). */
  draw:
    | Pick<PendingBboxDraw, "kind" | "bbox">
    | Pick<PendingPointDraw, "kind" | "point">;
}

export const VideoOverlayCanvas: React.FC<Props> = ({
  videoEl,
  cameraKey,
  objectAnnotations = [],
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const objectMaskCacheRef = useRef<Map<string, HTMLCanvasElement>>(new Map());
  const {
    atoms,
    setPendingDraw,
    pendingDraw,
    drawMode: ctxDrawMode,
    drawLabel,
    activeCamera,
    setActiveCamera,
    setActiveVideoEl,
    snap,
    addAtoms,
    clearPendingDraw,
    selectedIdx,
  } = useAnnotations();
  // Register this camera's <video> as the "active" one so the panel can read
  // its authoritative `currentTime` instead of the throttled context value
  // when adding annotations.
  useEffect(() => {
    if (activeCamera === cameraKey) {
      setActiveVideoEl(videoEl);
      return () => setActiveVideoEl(null);
    }
  }, [activeCamera, cameraKey, videoEl, setActiveVideoEl]);
  const drawMode = ctxDrawMode;
  const { currentTime, isPlaying } = useTime();
  // Pointer-down origin in canvas pixels and 0..1 image-relative coords.
  const dragOriginRef = useRef<{
    px: [number, number];
    norm: [number, number];
  } | null>(null);
  const [dragMoved, setDragMoved] = useState(false);
  const [finalizing, setFinalizing] = useState<FinalizingState | null>(null);
  const [labelInput, setLabelInput] = useState("");
  const [questionKind, setQuestionKind] = useState<"detect" | "point">(
    "detect",
  );

  // Keep the canvas exactly aligned with the video. Two listeners:
  //   - ResizeObserver picks up CSS resize.
  //   - `loadedmetadata` picks up the moment `video.videoWidth` becomes
  //     non-zero so `computeRenderedRect` stops returning the full-canvas
  //     fallback (which is what causes drawn bboxes to land in the wrong
  //     spot when the user starts annotating before metadata arrives).
  useEffect(() => {
    if (!videoEl || !canvasRef.current) return;
    const canvas = canvasRef.current;
    const sync = () => {
      const r = videoEl.getBoundingClientRect();
      // Use the device-pixel ratio for crisp drawing on HiDPI displays.
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(r.width));
      const h = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      redraw();
    };
    const ro = new ResizeObserver(sync);
    ro.observe(videoEl);
    videoEl.addEventListener("loadedmetadata", sync);
    videoEl.addEventListener("loadeddata", sync);
    sync();
    return () => {
      ro.disconnect();
      videoEl.removeEventListener("loadedmetadata", sync);
      videoEl.removeEventListener("loadeddata", sync);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoEl]);

  useEffect(() => {
    objectMaskCacheRef.current.clear();
  }, [objectAnnotations]);

  const redraw = React.useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !videoEl) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    // clearRect is in CSS-px after the dpr transform is applied; using the
    // bitmap dims here would clear a region too large but still works.
    ctx.clearRect(
      0,
      0,
      canvas.clientWidth || canvas.width,
      canvas.clientHeight || canvas.height,
    );
    const rect = computeRenderedRect(canvas, videoEl);

    // Object sidecars use the same episode-local clock as VQA atoms. Select
    // the nearest frame per track so a paused frame does not draw duplicate
    // masks when the browser time falls between source timestamps.
    const nearestObjects = new Map<string, ObjectAnnotation>();
    for (const annotation of objectAnnotations) {
      if (
        annotation.camera_key !== cameraKey ||
        annotation.status === "rejected"
      ) {
        continue;
      }
      const distance = Math.abs(annotation.timestamp - (currentTime || 0));
      if (distance > 0.08) continue;
      const key = `${annotation.object_id}:${annotation.track_id}`;
      const previous = nearestObjects.get(key);
      if (
        !previous ||
        distance < Math.abs(previous.timestamp - (currentTime || 0))
      ) {
        nearestObjects.set(key, annotation);
      }
    }
    for (const annotation of nearestObjects.values()) {
      const color = objectColor(annotation.track_id);
      drawObjectMask(ctx, rect, annotation, objectMaskCacheRef.current, color);
      drawObjectBbox(ctx, rect, annotation, color);
    }

    // Saved VQA atoms within ~one frame of currentTime. We compare against
    // the episode-local `currentTime` from useTime(), not the <video>'s
    // `currentTime`, because the latter is in *global* video-file time for
    // segmented (concatenated) videos.
    const selectedAtom =
      selectedIdx != null && selectedIdx >= 0 && selectedIdx < atoms.length
        ? atoms[selectedIdx]
        : null;
    const matches: LanguageAtom[] = atoms.filter((a, idx) => {
      if (a.style !== "vqa" || a.role !== "assistant") return false;
      const isSelectedAnswer =
        idx === selectedIdx ||
        (selectedAtom?.style === "vqa" &&
          selectedAtom.role === "user" &&
          selectedAtom.timestamp === a.timestamp &&
          selectedAtom.camera === a.camera);
      const isCurrentFrame = Math.abs(a.timestamp - (currentTime || 0)) < 0.05;
      if (!isCurrentFrame && !(isSelectedAnswer && !isPlaying)) return false;
      // Row-level camera is authoritative (lerobot PR 3467). Camera-agnostic
      // atoms (a.camera == null) draw on every camera.
      return a.camera == null || a.camera === cameraKey;
    });
    for (const atom of matches) {
      const ans = parseVqaAnswer(atom.content);
      if (!ans) continue;
      // For atoms that already carry a row-level camera tag, the filter above
      // is sufficient. The legacy in-payload camera field still matters for
      // pre-PR-3467 annotations the user may have on disk — keep the fallback
      // check so old datasets don't suddenly render on every camera.
      if (atom.camera == null && !vqaMatchesCamera(ans, cameraKey)) continue;
      const kind = classifyVqa(ans);
      if (kind === "bbox") {
        const dets = (ans as { detections: Array<unknown> })
          .detections as Array<{
          label?: string;
          bbox: [number, number, number, number];
          bbox_format?: string;
          camera?: string;
        }>;
        for (const d of dets) {
          if (d.camera && d.camera !== cameraKey) continue;
          drawBbox(
            ctx,
            rect,
            d.bbox,
            d.bbox_format || "xyxy",
            d.label || "",
            "#22d3ee",
          );
        }
      } else if (kind === "keypoint") {
        const k = ans as { point: [number, number]; label?: string };
        drawPoint(ctx, rect, k.point, k.label || "", "#facc15");
      }
    }

    // Pending (in-progress) draw for this camera.
    if (
      pendingDraw &&
      (!pendingDraw.camera || pendingDraw.camera === cameraKey)
    ) {
      if (pendingDraw.kind === "bbox") {
        drawBbox(
          ctx,
          rect,
          pendingDraw.bbox,
          "xyxy",
          pendingDraw.label || "",
          "#f97316",
        );
      } else {
        drawPoint(
          ctx,
          rect,
          pendingDraw.point,
          pendingDraw.label || "",
          "#f97316",
        );
      }
    }
  }, [
    atoms,
    objectAnnotations,
    pendingDraw,
    cameraKey,
    videoEl,
    currentTime,
    selectedIdx,
    isPlaying,
  ]);

  // Redraw on time tick / atoms / pendingDraw / videoEl changes.
  useEffect(() => {
    redraw();
  }, [redraw, currentTime]);

  // Also redraw the moment the video reports a seek completing — the
  // throttled `currentTime` from TimeContext can lag a paused frame by enough
  // that the overlay first paints empty. Listening directly to `seeked`
  // closes that gap so bbox/keypoint atoms appear instantly after jumping.
  useEffect(() => {
    if (!videoEl) return;
    const onSeeked = () => redraw();
    videoEl.addEventListener("seeked", onSeeked);
    return () => videoEl.removeEventListener("seeked", onSeeked);
  }, [videoEl, redraw]);

  // Pointer handlers. The disambiguation:
  //   - drawMode "auto":     drag (>4px) → bbox; release without dragging → keypoint
  //   - drawMode "bbox":     drag → bbox (no implicit keypoint)
  //   - drawMode "keypoint": down-then-up at same spot → keypoint
  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (drawMode === "off") return;
    const canvas = canvasRef.current;
    if (!canvas || !videoEl) return;
    if (finalizing) return; // wait for the user to confirm/cancel current draw
    e.preventDefault();
    setActiveCamera(cameraKey);
    setActiveVideoEl(videoEl);
    canvas.setPointerCapture(e.pointerId);
    const rect = computeRenderedRect(canvas, videoEl);
    const cr = canvas.getBoundingClientRect();
    const px: [number, number] = [e.clientX - cr.left, e.clientY - cr.top];
    const norm: [number, number] = [
      Math.max(0, Math.min(1, (px[0] - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (px[1] - rect.top) / rect.height)),
    ];
    dragOriginRef.current = { px, norm };
    setDragMoved(false);
    // Start a tentative bbox-shaped pendingDraw; if the user releases without
    // moving (auto/keypoint mode) we'll flip it to a keypoint on pointerup.
    setPendingDraw({
      kind: "bbox",
      bbox: [norm[0], norm[1], norm[0], norm[1]],
      label: drawLabel || "",
      camera: cameraKey,
    });
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (drawMode === "off" || !dragOriginRef.current) return;
    const canvas = canvasRef.current;
    if (!canvas || !videoEl) return;
    const rect = computeRenderedRect(canvas, videoEl);
    const cr = canvas.getBoundingClientRect();
    const cx = e.clientX - cr.left;
    const cy = e.clientY - cr.top;
    const dx = cx - dragOriginRef.current.px[0];
    const dy = cy - dragOriginRef.current.px[1];
    if (
      !dragMoved &&
      Math.hypot(dx, dy) > CLICK_THRESHOLD_PX &&
      drawMode !== "keypoint"
    ) {
      setDragMoved(true);
    }
    if (drawMode === "keypoint") return;
    const x = Math.max(0, Math.min(1, (cx - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (cy - rect.top) / rect.height));
    const start = dragOriginRef.current.norm;
    setPendingDraw({
      kind: "bbox",
      bbox: [
        Math.min(start[0], x),
        Math.min(start[1], y),
        Math.max(start[0], x),
        Math.max(start[1], y),
      ],
      label: drawLabel || "",
      camera: cameraKey,
    });
  };

  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (drawMode === "off" || !dragOriginRef.current) {
      dragOriginRef.current = null;
      setDragMoved(false);
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas || !videoEl) {
      dragOriginRef.current = null;
      setDragMoved(false);
      return;
    }
    const rect = computeRenderedRect(canvas, videoEl);
    const cr = canvas.getBoundingClientRect();
    const cx = e.clientX - cr.left;
    const cy = e.clientY - cr.top;

    // Decide the gesture's kind.
    const treatAsBbox =
      drawMode === "bbox" || (drawMode === "auto" && dragMoved);

    if (treatAsBbox) {
      const x = Math.max(0, Math.min(1, (cx - rect.left) / rect.width));
      const y = Math.max(0, Math.min(1, (cy - rect.top) / rect.height));
      const start = dragOriginRef.current.norm;
      const bbox: [number, number, number, number] = [
        Math.min(start[0], x),
        Math.min(start[1], y),
        Math.max(start[0], x),
        Math.max(start[1], y),
      ];
      setPendingDraw({
        kind: "bbox",
        bbox,
        label: drawLabel || "",
        camera: cameraKey,
      });
      setFinalizing({ draw: { kind: "bbox", bbox } });
      setQuestionKind("detect");
    } else {
      // Click → keypoint at the up position.
      const x = Math.max(0, Math.min(1, (cx - rect.left) / rect.width));
      const y = Math.max(0, Math.min(1, (cy - rect.top) / rect.height));
      const point: [number, number] = [x, y];
      setPendingDraw({
        kind: "keypoint",
        point,
        label: drawLabel || "",
        camera: cameraKey,
      });
      setFinalizing({ draw: { kind: "keypoint", point } });
      setQuestionKind("point");
    }
    dragOriginRef.current = null;
    setDragMoved(false);
  };

  const onPointerCancel = () => {
    dragOriginRef.current = null;
    setDragMoved(false);
  };

  const closeFinalize = React.useCallback(() => {
    setFinalizing(null);
    setLabelInput("");
    clearPendingDraw();
  }, [clearPendingDraw]);

  const submitFinalize = () => {
    if (!finalizing) return;
    const label = labelInput.trim();
    if (!label) return;
    // Episode-local time only — `videoEl.currentTime` is the *global* time
    // inside a shared/concatenated video file, which would push every
    // annotation past the parquet's [0..duration] frame range and collapse
    // them to the boundary on snap. `currentTime` from useTime() is already
    // normalized to episode-local space by SimpleVideosPlayer.
    const ts = snap(currentTime);
    const question =
      questionKind === "point"
        ? `Point to the ${label}.`
        : `Where is the ${label} in the image?`;
    let answer:
      | {
          detections: Array<{
            label: string;
            bbox_format: "xyxy";
            bbox: [number, number, number, number];
            camera?: string;
          }>;
        }
      | {
          label: string;
          point_format: "xy";
          point: [number, number];
          camera?: string;
        };
    if (finalizing.draw.kind === "bbox") {
      answer = {
        detections: [
          {
            label,
            bbox_format: "xyxy",
            bbox: finalizing.draw.bbox.map((v) => Number(v.toFixed(4))) as [
              number,
              number,
              number,
              number,
            ],
            camera: cameraKey,
          },
        ],
      };
    } else {
      answer = {
        label,
        point_format: "xy",
        point: finalizing.draw.point.map((v) => Number(v.toFixed(4))) as [
          number,
          number,
        ],
        camera: cameraKey,
      };
    }
    addAtoms([
      {
        role: "user",
        content: question,
        style: "vqa",
        timestamp: ts,
        camera: cameraKey,
        tool_calls: null,
      },
      {
        role: "assistant",
        content: JSON.stringify(answer),
        style: "vqa",
        timestamp: ts,
        camera: cameraKey,
        tool_calls: null,
      },
    ]);
    closeFinalize();
  };

  // ESC / click-outside to cancel the popup.
  useEffect(() => {
    if (!finalizing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeFinalize();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [finalizing, closeFinalize]);

  return (
    <T>
      {
        <>
          <canvas
            ref={canvasRef}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerCancel}
            style={{
              position: "absolute",
              inset: 0,
              pointerEvents: drawMode === "off" ? "none" : "auto",
              cursor:
                drawMode === "off"
                  ? "default"
                  : drawMode === "keypoint"
                    ? "pointer"
                    : "crosshair",
            }}
          />
          {finalizing && (
            <QuickLabelPopup
              kind={finalizing.draw.kind}
              questionKind={questionKind}
              onQuestionKindChange={setQuestionKind}
              label={labelInput}
              onLabelChange={setLabelInput}
              onSubmit={submitFinalize}
              onCancel={closeFinalize}
            />
          )}
        </>
      }
    </T>
  );
};

/**
 * Centered "what is this?" popup for a freshly drawn bbox or keypoint.
 * The label gets templated into a question — bbox → "Where is the X
 * in the image?", keypoint → "Point to the X." — and the assistant message
 * carries the JSON answer the steerable validator expects.
 */
const QuickLabelPopup: React.FC<{
  kind: "bbox" | "keypoint";
  questionKind: "detect" | "point";
  onQuestionKindChange: (k: "detect" | "point") => void;
  label: string;
  onLabelChange: (s: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}> = ({
  kind,
  questionKind,
  onQuestionKindChange,
  label,
  onLabelChange,
  onSubmit,
  onCancel,
}) => {
  const { t } = useLocale();
  const inputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  return (
    <T>
      {
        <DraggablePopup
          header={
            <>
              <span className={"kind-pill " + kind}>
                <T>{kind}</T>
              </span>
              <select
                value={questionKind}
                onChange={(e) =>
                  onQuestionKindChange(e.target.value as "detect" | "point")
                }
                style={{ marginLeft: "auto" }}
              >
                <option value="detect">
                  <T>where is …?</T>
                </option>
                <option value="point">
                  <T>point to …</T>
                </option>
              </select>
            </>
          }
          ariaLabel="Create visual annotation"
          canSubmit={label.trim().length > 0}
          onSubmit={onSubmit}
          onCancel={onCancel}
        >
          <input
            ref={inputRef}
            type="text"
            placeholder={t(
              kind === "bbox" ? "label (e.g. carrot)" : "label (e.g. handle)",
            )}
            value={label}
            onChange={(e) => onLabelChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onSubmit();
            }}
          />
          <div className="quick-popup-actions">
            <button onClick={onCancel} className="popup-btn">
              <T>cancel</T>
            </button>
            <button
              onClick={onSubmit}
              disabled={!label.trim()}
              className="popup-btn primary"
            >
              <T>add ↵</T>
            </button>
          </div>
        </DraggablePopup>
      }
    </T>
  );
};
