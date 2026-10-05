// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T, useLocale } from "@/components/levi-locale";
import {
  Button,
  EmptyState,
  Icon,
  IconButton,
  SegmentedControl,
  Select,
  Spinner,
} from "@/components/ds";
import { ChevronLeft, ChevronRight, Flag, ImageOff } from "lucide-react";

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
        <div ref={containerRef} className="vw-thumb">
          <div className="vw-thumb-frame group">
            <T>
              {inView ? (
                <video
                  ref={videoRef}
                  src={info.videoUrl}
                  preload="metadata"
                  muted
                  className="vw-thumb-media"
                />
              ) : (
                <div
                  className="vw-thumb-media vw-thumb-skeleton"
                  aria-hidden="true"
                />
              )}
            </T>
            <span className="vw-thumb-flag-slot">
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
          <p className="vw-thumb-caption" data-flagged={isFlagged || undefined}>
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
      <div className="vw-a-view" role="status">
        <Spinner label={t("Loading episode frames…")} showLabel />
      </div>
    );
  }

  const allFrames = data.framesByCamera[selectedCamera] ?? [];
  const frames = flaggedOnly
    ? allFrames.filter((f) => flagged.has(f.episodeIndex))
    : allFrames;

  if (frames.length === 0) {
    return (
      <EmptyState
        icon={ImageOff}
        title={t(
          flaggedOnly
            ? "No flagged episodes to show."
            : "No episode frames available.",
        )}
        action={
          flaggedOnly && onFlaggedOnlyChange ? (
            <Button size="sm" onClick={() => onFlaggedOnlyChange(false)}>
              {t("Show all episodes")}
            </Button>
          ) : undefined
        }
      />
    );
  }

  const totalPages = Math.ceil(frames.length / PAGE_SIZE);
  const pageFrames = frames.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  return (
    <T>
      {
        <div className="vw-a-view vw-a-view--wide">
          <p className="vw-a-hint">
            <T>
              Use first/last frame views to spot episodes with bad end states or
              other anomalies. Hover over a thumbnail and click the flag icon to
              mark episodes with wrong outcomes for review.
            </T>
          </p>

          {/* Controls row */}
          <div className="vw-a-row">
            <div className="vw-a-row">
              {/* Camera selector */}
              {data.cameras.length > 1 && (
                <Select
                  aria-label={t("Camera")}
                  value={selectedCamera}
                  onChange={handleCameraChange}
                  className="vw-camera-select"
                >
                  {data.cameras.map((cam) => (
                    <option key={cam} value={cam}>
                      {cam}
                    </option>
                  ))}
                </Select>
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
              <div className="vw-pager">
                <Button
                  size="sm"
                  variant="ghost"
                  icon={ChevronLeft}
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                >
                  {t("Previous")}
                </Button>
                <span>
                  {page + 1} / {totalPages}
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
          <div className="vw-thumb-grid">
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
