"use client";

import React, { useEffect, useRef } from "react";
import type { LiveResult } from "@/types/segmentation.types";
import {
  chooseResult,
  decodeRle,
  displayedFrame,
  liveResults,
  liveSession,
  registerLiveVideo,
  subscribeLive,
} from "@/utils/liveSegmentation";

const TRACK_COLORS = [
  "#22d3ee",
  "#a78bfa",
  "#34d399",
  "#fbbf24",
  "#fb7185",
  "#60a5fa",
  "#f472b6",
  "#a3e635",
  "#fb923c",
  "#2dd4bf",
];

export function trackColor(trackId: number): string {
  return TRACK_COLORS[Math.abs(trackId) % TRACK_COLORS.length];
}

function rgb(hex: string): [number, number, number] {
  return [
    Number.parseInt(hex.slice(1, 3), 16),
    Number.parseInt(hex.slice(3, 5), 16),
    Number.parseInt(hex.slice(5, 7), 16),
  ];
}

/** Every mask of one result, painted once (fill + outline) at mask size. */
const maskCache = new WeakMap<LiveResult, HTMLCanvasElement | null>();

function resultMasks(result: LiveResult): HTMLCanvasElement | null {
  if (maskCache.has(result)) return maskCache.get(result) ?? null;
  const first = result.objects.find((o) => o.mask_rle)?.mask_rle;
  const [height, width] = first?.size ?? [0, 0];
  let canvas: HTMLCanvasElement | null = null;
  if (height > 0 && width > 0) {
    canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (ctx) {
      const image = ctx.createImageData(width, height);
      for (const object of result.objects) {
        const rle = object.mask_rle;
        if (!rle || rle.size[0] !== height || rle.size[1] !== width) continue;
        const mask = decodeRle(rle).data;
        const [r, g, b] = rgb(trackColor(object.track_id));
        // Scan only the object's box (padded) instead of the whole frame.
        const [bx1, by1, bx2, by2] = object.bbox_xyxy;
        let sx0 = Math.max(0, Math.floor(bx1) - 1);
        let sy0 = Math.max(0, Math.floor(by1) - 1);
        let sx1 = Math.min(width - 1, Math.ceil(bx2) + 1);
        let sy1 = Math.min(height - 1, Math.ceil(by2) + 1);
        if (!(sx1 >= sx0 && sy1 >= sy0)) {
          [sx0, sy0, sx1, sy1] = [0, 0, width - 1, height - 1];
        }
        for (let y = sy0; y <= sy1; y += 1)
          for (let x = sx0; x <= sx1; x += 1) {
            const i = y * width + x;
            if (!mask[i]) continue;
            const edge =
              x === 0 ||
              y === 0 ||
              x === width - 1 ||
              y === height - 1 ||
              !mask[i - 1] ||
              !mask[i + 1] ||
              !mask[i - width] ||
              !mask[i + width];
            const p = i * 4;
            image.data[p] = r;
            image.data[p + 1] = g;
            image.data[p + 2] = b;
            image.data[p + 3] = edge ? 255 : 96;
          }
      }
      ctx.putImageData(image, 0, 0);
    } else {
      canvas = null;
    }
  }
  maskCache.set(result, canvas);
  return canvas;
}

/** Where the `object-contain` video image sits inside the element box. */
function contentRect(cssW: number, cssH: number, vw: number, vh: number) {
  if (!vw || !vh) return { left: 0, top: 0, width: cssW, height: cssH };
  const aspect = vw / vh;
  if (cssW / cssH > aspect) {
    const width = cssH * aspect;
    return { left: (cssW - width) / 2, top: 0, width, height: cssH };
  }
  const height = cssW / aspect;
  return { left: 0, top: (cssH - height) / 2, width: cssW, height };
}

function draw(
  canvas: HTMLCanvasElement,
  video: HTMLVideoElement,
  result: LiveResult | null,
) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const dpr = canvas.width / Math.max(1, canvas.clientWidth || 1);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const cssW = canvas.clientWidth;
  const cssH = canvas.clientHeight;
  ctx.clearRect(0, 0, cssW, cssH);
  if (!result || !result.objects.length) return;
  const rect = contentRect(cssW, cssH, video.videoWidth, video.videoHeight);
  const masks = resultMasks(result);
  if (masks) ctx.drawImage(masks, rect.left, rect.top, rect.width, rect.height);
  const [imageH, imageW] = result.image_size ??
    result.objects.find((o) => o.mask_rle)?.mask_rle?.size ?? [
      video.videoHeight,
      video.videoWidth,
    ];
  if (!imageW || !imageH) return;
  ctx.font = "11px ui-sans-serif, system-ui";
  for (const object of result.objects) {
    const color = trackColor(object.track_id);
    const [x1, y1, x2, y2] = object.bbox_xyxy;
    const px = rect.left + (x1 / imageW) * rect.width;
    const py = rect.top + (y1 / imageH) * rect.height;
    if (!masks) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.strokeRect(
        px,
        py,
        ((x2 - x1) / imageW) * rect.width,
        ((y2 - y1) / imageH) * rect.height,
      );
    }
    const label = `${object.concept} #${object.track_id}`;
    const labelW = ctx.measureText(label).width + 8;
    const top = Math.max(0, py - 16);
    ctx.fillStyle = "#0b0e14d9";
    ctx.fillRect(px, top, labelW, 16);
    ctx.fillStyle = color;
    ctx.fillText(label, px + 4, top + 12);
  }
}

type Props = {
  videoEl: HTMLVideoElement | null;
  cameraKey: string;
  episodeId?: number;
  /** Start of this episode inside a shared (v3) video file, in seconds. */
  segmentStart?: number;
};

/** Live segmentation overlay for one camera. Its own animation-frame loop
 * reads `video.currentTime` directly and redraws only when the chosen
 * result or the canvas size changes. */
export const LiveSegmentationCanvas: React.FC<Props> = ({
  videoEl,
  cameraKey,
  episodeId,
  segmentStart = 0,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!videoEl || !canvas) return;
    const unregister = registerLiveVideo(cameraKey, videoEl, segmentStart);
    let raf = 0;
    let drawn: LiveResult | null | undefined;
    let drawnSize = "";

    const resize = () => {
      const r = videoEl.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(r.width));
      const h = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${h}px`;
      drawnSize = "";
    };

    const active = () => {
      const session = liveSession();
      return (
        !!session.sessionId &&
        (episodeId == null || session.episodeIndex === episodeId)
      );
    };

    const tick = () => {
      raf = 0;
      if (!active()) {
        clear();
        return;
      }
      const { fps } = liveSession();
      const frame = displayedFrame(videoEl.currentTime, fps, segmentStart);
      const result = chooseResult(liveResults(cameraKey), frame);
      const size = `${canvas.width}x${canvas.height}:${videoEl.videoWidth}x${videoEl.videoHeight}`;
      if (result !== drawn || size !== drawnSize) {
        draw(canvas, videoEl, result);
        drawn = result;
        drawnSize = size;
        canvas.dataset.liveFrame = String(result ? result.frame_index : -1);
      }
      if (canvas.dataset.displayFrame !== String(frame))
        canvas.dataset.displayFrame = String(frame);
      raf = requestAnimationFrame(tick);
    };

    const clear = () => {
      if (drawn === undefined) return;
      canvas.getContext("2d")?.clearRect(0, 0, canvas.width, canvas.height);
      drawn = undefined;
      canvas.dataset.liveFrame = "-1";
    };

    const onStore = () => {
      if (active()) {
        if (!raf) raf = requestAnimationFrame(tick);
      } else {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        clear();
      }
    };

    const ro = new ResizeObserver(resize);
    ro.observe(videoEl);
    videoEl.addEventListener("loadedmetadata", resize);
    resize();
    const unsubscribe = subscribeLive(onStore);
    onStore();
    return () => {
      if (raf) cancelAnimationFrame(raf);
      unsubscribe();
      unregister();
      ro.disconnect();
      videoEl.removeEventListener("loadedmetadata", resize);
    };
  }, [videoEl, cameraKey, episodeId, segmentStart]);

  return (
    <canvas
      ref={canvasRef}
      data-testid="live-seg-canvas"
      data-camera={cameraKey}
      data-live-frame="-1"
      aria-hidden="true"
      style={{ position: "absolute", inset: 0, pointerEvents: "none" }}
    />
  );
};

export default LiveSegmentationCanvas;
