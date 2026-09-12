// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { T } from "@/components/levi-locale";

import Link from "next/link";
import React, { useMemo, useState } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";

import type { DatasetDisplayInfo } from "@/app/[org]/[dataset]/[episode]/fetch-data";
import type { AnnotationSummary } from "@/utils/annotationsClient";

/** Small status dots distinguishing language/event annotations from SAM3
 * object/vision annotations — deliberately two separate marks, not one
 * combined dot, so multi-track annotation work stays visually distinct. */
function AnnotationDots({
  episode,
  summary,
}: {
  episode: number;
  summary: AnnotationSummary;
}) {
  const key = String(episode);
  const hasLanguage = !!summary.language[key];
  const hasVision = !!summary.vision[key];
  if (!hasLanguage && !hasVision) return null;
  return (
    <span className="flex items-center gap-1 shrink-0">
      {hasLanguage && (
        <span
          className="w-1.5 h-1.5 rounded-full bg-cyan-400"
          title="Has language/event annotations"
        />
      )}
      {hasVision && (
        <span
          className="w-1.5 h-1.5 rounded-full bg-lime-400"
          title="Has object/vision (SAM3) annotations"
        />
      )}
    </span>
  );
}

interface SidebarProps {
  datasetInfo: DatasetDisplayInfo;
  paginatedEpisodes: number[];
  episodeId: number;
  totalPages: number;
  currentPage: number;
  prevPage: () => void;
  nextPage: () => void;
  showFlaggedOnly: boolean;
  onShowFlaggedOnlyChange: (v: boolean) => void;
  onEpisodeSelect?: (ep: number) => void;
  /** Dataset task strings; the filter appears once there are at least two. */
  tasks?: string[];
  /** Active task filter, or null for every task. */
  taskFilter?: string | null;
  onTaskFilterChange?: (task: string | null) => void;
  /** Episodes left after the task filter, across all pages. */
  filteredEpisodeCount?: number;
  /** Per-episode language/vision annotation presence, for the status dots. */
  annotationSummary?: AnnotationSummary;
}

const Sidebar: React.FC<SidebarProps> = ({
  datasetInfo,
  paginatedEpisodes,
  episodeId,
  totalPages,
  currentPage,
  prevPage,
  nextPage,
  showFlaggedOnly,
  onShowFlaggedOnlyChange,
  onEpisodeSelect,
  tasks = [],
  taskFilter = null,
  onTaskFilterChange,
  filteredEpisodeCount,
  annotationSummary,
}) => {
  const [mobileVisible, setMobileVisible] = useState(false);
  const { flagged, count, toggle } = useFlaggedEpisodes();

  const displayEpisodes = useMemo(() => {
    if (!showFlaggedOnly || count === 0) return paginatedEpisodes;
    return [...flagged].sort((a, b) => a - b);
  }, [paginatedEpisodes, showFlaggedOnly, flagged, count]);

  return (
    <T>
      {
        <div className="flex z-10 shrink-0">
          <nav
            className={`shrink-0 overflow-y-auto bg-[var(--surface-0)] border-r border-white/5 p-4 break-words w-60 ${
              mobileVisible ? "block" : "hidden"
            } md:block`}
            aria-label="Sidebar navigation"
          >
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs text-slate-400 tabular">
              <dt className="uppercase tracking-wide text-[10px] text-slate-500">
                <T>Frames</T>
              </dt>
              <dd className="text-slate-200">
                {datasetInfo.total_frames.toLocaleString()}
              </dd>
              <dt className="uppercase tracking-wide text-[10px] text-slate-500">
                <T>Episodes</T>
              </dt>
              <dd className="text-slate-200">
                {datasetInfo.total_episodes.toLocaleString()}
              </dd>
              <dt className="uppercase tracking-wide text-[10px] text-slate-500">
                <T>FPS</T>
              </dt>
              <dd className="text-slate-200">
                <T>{datasetInfo.fps}</T>
              </dd>
            </dl>

            {tasks.length > 1 && onTaskFilterChange && (
              <div className="mt-5">
                <p className="text-[10px] uppercase tracking-wide text-slate-500">
                  <T>Task filter</T>
                </p>
                <select
                  value={taskFilter ?? ""}
                  onChange={(e) => onTaskFilterChange(e.target.value || null)}
                  className="mt-1 w-full bg-[var(--surface-1)] border border-white/10 rounded-md px-2 py-1 text-xs text-slate-200"
                  aria-label="Filter episodes by task"
                >
                  <option value="">{`All tasks (${tasks.length})`}</option>
                  {tasks.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            <div className="mt-5 flex items-center justify-between">
              <p className="text-[10px] uppercase tracking-wide text-slate-500">
                <T>Episodes</T>
                {taskFilter && filteredEpisodeCount !== undefined && (
                  <span className="ml-1 text-slate-400 tabular">
                    <T>{`· ${filteredEpisodeCount}`}</T>
                  </span>
                )}
              </p>
              {count > 0 && (
                <button
                  onClick={() => onShowFlaggedOnlyChange(!showFlaggedOnly)}
                  className={`text-[10px] uppercase tracking-wide px-2 py-0.5 rounded-md transition-colors ${
                    showFlaggedOnly
                      ? "bg-orange-500/15 text-orange-300 border border-orange-500/30"
                      : "text-slate-500 hover:text-slate-300 border border-white/10"
                  }`}
                >
                  <T>Flagged · </T>
                  <T>{count}</T>
                </button>
              )}
            </div>

            {displayEpisodes.length === 0 && (
              <p className="mt-2 text-xs text-slate-500 italic">
                <T>No episodes match this task.</T>
              </p>
            )}

            <ul className="mt-2 space-y-px">
              {displayEpisodes.map((episode) => {
                const active = episode === episodeId;
                const itemClass = `group flex items-center justify-between gap-2 px-2 py-1 rounded-md text-xs tabular transition-colors ${
                  active
                    ? "bg-cyan-400/10 text-cyan-300"
                    : "text-slate-300 hover:bg-white/5"
                }`;
                return (
                  <li key={episode}>
                    <T>
                      {onEpisodeSelect ? (
                        <div className={itemClass}>
                          <button
                            onClick={() => onEpisodeSelect(episode)}
                            className="flex-1 text-left"
                          >
                            Episode {episode}
                          </button>
                          {annotationSummary && (
                            <AnnotationDots
                              episode={episode}
                              summary={annotationSummary}
                            />
                          )}
                          <button
                            onClick={() => toggle(episode)}
                            className={`text-xs leading-none transition-colors ${
                              flagged.has(episode)
                                ? "text-orange-400 hover:text-orange-300"
                                : "text-slate-600 hover:text-slate-400 opacity-0 group-hover:opacity-100"
                            }`}
                            title={flagged.has(episode) ? "Unflag" : "Flag"}
                          >
                            ⚑
                          </button>
                        </div>
                      ) : (
                        <div className={itemClass}>
                          <Link
                            href={`./episode_${episode}`}
                            className="flex-1 text-left"
                          >
                            Episode {episode}
                          </Link>
                          {annotationSummary && (
                            <AnnotationDots
                              episode={episode}
                              summary={annotationSummary}
                            />
                          )}
                          <button
                            onClick={() => toggle(episode)}
                            className={`text-xs leading-none transition-colors ${
                              flagged.has(episode)
                                ? "text-orange-400 hover:text-orange-300"
                                : "text-slate-600 hover:text-slate-400 opacity-0 group-hover:opacity-100"
                            }`}
                            title={flagged.has(episode) ? "Unflag" : "Flag"}
                          >
                            ⚑
                          </button>
                        </div>
                      )}
                    </T>
                  </li>
                );
              })}
            </ul>

            {!showFlaggedOnly && totalPages > 1 && (
              <div className="mt-3 flex items-center gap-2 text-[10px] uppercase tracking-wide text-slate-400">
                <button
                  onClick={prevPage}
                  className={`px-2 py-1 rounded-md border border-white/10 transition-colors hover:bg-white/5 hover:text-slate-200 ${
                    currentPage === 1 ? "cursor-not-allowed opacity-40" : ""
                  }`}
                  disabled={currentPage === 1}
                >
                  <T>‹ Prev</T>
                </button>
                <span className="tabular text-slate-500">
                  <T>{currentPage}</T> / <T>{totalPages}</T>
                </span>
                <button
                  onClick={nextPage}
                  className={`ml-auto px-2 py-1 rounded-md border border-white/10 transition-colors hover:bg-white/5 hover:text-slate-200 ${
                    currentPage === totalPages
                      ? "cursor-not-allowed opacity-40"
                      : ""
                  }`}
                  disabled={currentPage === totalPages}
                >
                  <T>Next ›</T>
                </button>
              </div>
            )}
          </nav>

          <button
            className="mx-1 flex items-center opacity-50 hover:opacity-100 focus:outline-none focus:ring-0 md:hidden"
            onClick={() => setMobileVisible((prev) => !prev)}
            title="Toggle sidebar"
          >
            <div className="h-10 w-1 rounded-full bg-white/20" />
          </button>
        </div>
      }
    </T>
  );
};

export default Sidebar;
