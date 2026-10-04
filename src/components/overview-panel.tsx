// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";
import { Button, Icon, IconButton, SegmentedControl } from "@/components/ds";
import { ChevronLeft, ChevronRight, Flag, LoaderCircle } from "lucide-react";

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
  const { t } = useLocale();
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
            <span className="absolute top-1 right-1">
              <IconButton
                icon={Flag}
                size="sm"
                variant="secondary"
                className="vw-flag-btn vw-thumb-flag"
                pressed={isFlagged}
                label={t(isFlagged ? "Unflag episode" : "Flag episode")}
                onClick={() => toggle(info.episodeIndex)}
              />
            </span>
          </div>
          <p
            className={`text-xs mt-1 tabular-nums ${isFlagged ? "text-(--ds-text-primary)" : "text-(--ds-text-secondary)"}`}
          >
            {t(`Episode ${info.episodeIndex}`)}
            {isFlagged && (
              <Icon icon={Flag} label={t("Flagged")} className="ml-1 inline" />
            )}
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
            <p className="text-(--ds-text-secondary) italic">
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
          <p className="text-sm text-(--ds-text-secondary)">
            <T>
              Use first/last frame views to spot episodes with bad end states or
              other anomalies. Hover over a thumbnail and click the flag icon to
              mark episodes with wrong outcomes for review.
            </T>
          </p>

          {/* Controls row */}
          <div className="flex items-center justify-between flex-wrap gap-4">
            <div className="flex flex-wrap items-center gap-3">
              {/* Camera selector */}
              {data.cameras.length > 1 && (
                <select
                  aria-label="Camera"
                  value={selectedCamera}
                  onChange={handleCameraChange}
                  className="ds-input w-auto"
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
                  type="button"
                  className="vw-chip ds-focus"
                  aria-pressed={flaggedOnly}
                  onClick={() => {
                    onFlaggedOnlyChange(!flaggedOnly);
                    setPage(0);
                  }}
                >
                  <Icon icon={Flag} />
                  {t("Flagged only")} · {flagCount}
                </button>
              )}

              {/* First / Last frame */}
              <SegmentedControl
                label={t("Frame shown")}
                size="sm"
                className="vw-nowrap"
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
                <Button
                  size="sm"
                  variant="ghost"
                  icon={ChevronLeft}
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  {t("Previous")}
                </Button>
                <span className="tabular-nums">
                  {page + 1} / <T>{totalPages}</T>
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  iconEnd={ChevronRight}
                  disabled={page === totalPages - 1}
                  onClick={() => setPage((p) => p + 1)}
                >
                  {t("Next")}
                </Button>
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
