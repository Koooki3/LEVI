// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { Icon } from "@/components/ds";
import { LoaderCircle } from "lucide-react";
import { T, useLocale } from "@/components/levi-locale";
import { SegmentedControl } from "@/components/ds";

import React, { useState, useEffect, useRef, useCallback } from "react";
import type {
  EpisodeFrameInfo,
  EpisodeFramesData,
} from "@/app/[org]/[dataset]/[episode]/fetch-data";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";

const PAGE_SIZE = 48;

function FrameThumbnail({
  info,
  showLast,
}: {
  info: EpisodeFrameInfo;
  showLast: boolean;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [inView, setInView] = useState(false);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) {
          setInView(true);
          obs.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !inView) return;

    const seek = () => {
      if (showLast) {
        video.currentTime =
          info.lastFrameTime ?? Math.max(0, video.duration - 0.05);
      } else {
        video.currentTime = info.firstFrameTime;
      }
    };

    if (video.readyState >= 1) {
      seek();
    } else {
      video.addEventListener("loadedmetadata", seek, { once: true });
      return () => video.removeEventListener("loadedmetadata", seek);
    }
  }, [inView, showLast, info]);

  const { has, toggle } = useFlaggedEpisodes();
  const isFlagged = has(info.episodeIndex);

  return (
    <T>
      {
        <div ref={containerRef} className="flex flex-col items-center">
          <div className="w-full aspect-video bg-(--ds-surface-1) rounded overflow-hidden relative group">
            <T>
              {inView ? (
                <video
                  ref={videoRef}
                  src={info.videoUrl}
                  preload="metadata"
                  muted
                  className="w-full h-full object-cover"
                />
              ) : (
                <div
                  className="w-full h-full bg-(--ds-skeleton)"
                  aria-hidden="true"
                />
              )}
            </T>
            <button
              onClick={() => toggle(info.episodeIndex)}
              className={`absolute top-1 right-1 p-1 rounded transition-opacity ${
                isFlagged
                  ? "opacity-100 text-(--ds-text-primary)"
                  : "opacity-0 group-hover:opacity-100 text-(--ds-text-secondary) hover:text-(--ds-text-primary)"
              }`}
              title={isFlagged ? "Unflag episode" : "Flag episode"}
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill={isFlagged ? "currentColor" : "none"}
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
                <line x1="4" y1="22" x2="4" y2="15" />
              </svg>
            </button>
          </div>
          <p
            className={`text-xs mt-1 tabular-nums ${isFlagged ? "text-(--ds-text-primary)" : "text-(--ds-text-secondary)"}`}
          >
            <T>ep </T>
            <T>{info.episodeIndex}</T>
            <T>{isFlagged ? " ⚑" : ""}</T>
          </p>
        </div>
      }
    </T>
  );
}

interface OverviewPanelProps {
  data: EpisodeFramesData | null;
  loading: boolean;
  flaggedOnly?: boolean;
  onFlaggedOnlyChange?: (v: boolean) => void;
}

export default function OverviewPanel({
  data,
  loading,
  flaggedOnly = false,
  onFlaggedOnlyChange,
}: OverviewPanelProps) {
  const { t } = useLocale();
  const { flagged, count: flagCount } = useFlaggedEpisodes();
  const [selectedCamera, setSelectedCamera] = useState<string>("");
  const [showLast, setShowLast] = useState(false);
  const [page, setPage] = useState(0);

  // Auto-select first camera when data arrives
  useEffect(() => {
    if (data && data.cameras.length > 0 && !selectedCamera) {
      setSelectedCamera(data.cameras[0]);
    }
  }, [data, selectedCamera]);

  const handleCameraChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      setSelectedCamera(e.target.value);
      setPage(0);
    },
    [],
  );

  if (loading || !data) {
    return (
      <T>
        {
          <div className="flex items-center gap-2 text-(--ds-text-secondary) text-sm py-12 justify-center">
            <Icon icon={LoaderCircle} className="ds-spin" />
            <T>Loading episode frames…</T>
          </div>
        }
      </T>
    );
  }

  const allFrames = data.framesByCamera[selectedCamera] ?? [];
  const frames = flaggedOnly
    ? allFrames.filter((f) => flagged.has(f.episodeIndex))
    : allFrames;

  if (frames.length === 0) {
    return (
      <T>
        {
          <div className="text-center py-8 space-y-2">
            <p className="text-(--ds-text-tertiary) italic">
              <T>
                {flaggedOnly
                  ? "No flagged episodes to show."
                  : "No episode frames available."}
              </T>
            </p>
            {flaggedOnly && onFlaggedOnlyChange && (
              <button
                onClick={() => onFlaggedOnlyChange(false)}
                className="text-xs text-(--ds-text-primary) hover:text-(--ds-text-primary) underline"
              >
                <T>Show all episodes</T>
              </button>
            )}
          </div>
        }
      </T>
    );
  }

  const totalPages = Math.ceil(frames.length / PAGE_SIZE);
  const pageFrames = frames.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <T>
      {
        <div className="max-w-7xl mx-auto py-6 space-y-5">
          <p className="text-sm text-(--ds-text-tertiary)">
            <T>
              Use first/last frame views to spot episodes with bad end states or
              other anomalies. Hover over a thumbnail and click the flag icon to
              mark episodes with wrong outcomes for review.
            </T>
          </p>

          {/* Controls row */}
          <div className="flex items-center justify-between flex-wrap gap-4">
            <div className="flex items-center gap-5">
              {/* Camera selector */}
              {data.cameras.length > 1 && (
                <select
                  value={selectedCamera}
                  onChange={handleCameraChange}
                  className="bg-(--ds-surface-1) text-(--ds-text-primary) text-sm rounded px-3 py-1.5 border border-(--ds-separator) focus:outline-none focus:border-(--ds-accent)"
                >
                  {data.cameras.map((cam) => (
                    <option key={cam} value={cam}>
                      <T>{cam}</T>
                    </option>
                  ))}
                </select>
              )}

              {/* Flagged only toggle */}
              {flagCount > 0 && onFlaggedOnlyChange && (
                <button
                  onClick={() => {
                    onFlaggedOnlyChange(!flaggedOnly);
                    setPage(0);
                  }}
                  className={`text-xs px-2.5 py-1 rounded transition-colors flex items-center gap-1.5 ${
                    flaggedOnly
                      ? "bg-(--ds-surface-selected) text-(--ds-text-primary) border border-(--ds-accent)"
                      : "text-(--ds-text-secondary) hover:text-(--ds-text-primary) border border-(--ds-separator)"
                  }`}
                >
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    width="12"
                    height="12"
                    viewBox="0 0 24 24"
                    fill={flaggedOnly ? "currentColor" : "none"}
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
                    <line x1="4" y1="22" x2="4" y2="15" />
                  </svg>
                  <T>Flagged only (</T>
                  <T>{flagCount}</T>)
                </button>
              )}

              {/* First / Last frame */}
              <SegmentedControl
                label={t("Frame shown")}
                size="sm"
                value={showLast ? "last" : "first"}
                onChange={(value) => setShowLast(value === "last")}
                options={[
                  { value: "first", label: t("First Frame") },
                  { value: "last", label: t("Last Frame") },
                ]}
              />
            </div>

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center gap-2 text-sm text-(--ds-text-secondary)">
                <button
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                  className="px-2 py-1 rounded bg-(--ds-surface-1) hover:bg-(--ds-surface-sunken) disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <T>← Prev</T>
                </button>
                <span className="tabular-nums">
                  {page + 1} / <T>{totalPages}</T>
                </span>
                <button
                  disabled={page === totalPages - 1}
                  onClick={() => setPage((p) => p + 1)}
                  className="px-2 py-1 rounded bg-(--ds-surface-1) hover:bg-(--ds-surface-sunken) disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <T>Next →</T>
                </button>
              </div>
            )}
          </div>

          {/* Adaptive grid — only current page's thumbnails are mounted */}
          <div
            className="grid gap-3"
            style={{
              gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
            }}
          >
            {pageFrames.map((info) => (
              <FrameThumbnail
                key={`${selectedCamera}-${info.episodeIndex}`}
                info={info}
                showLast={showLast}
              />
            ))}
          </div>
        </div>
      }
    </T>
  );
}
