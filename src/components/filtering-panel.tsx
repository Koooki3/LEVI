// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { Flag, LoaderCircle } from "lucide-react";
import { Icon, IconButton } from "@/components/ds";
import { T, useLocale } from "@/components/levi-locale";

import React, { useState, useMemo, useCallback } from "react";
import { useFlaggedEpisodes } from "@/context/flagged-episodes-context";
import type {
  CrossEpisodeVarianceData,
  LowMovementEpisode,
  EpisodeLengthStats,
  EpisodeLengthInfo,
} from "@/app/[org]/[dataset]/[episode]/fetch-data";
import {
  ActionVelocitySection,
  FullscreenWrapper,
} from "@/components/action-insights-panel";

// ─── Shared small components ─────────────────────────────────────

function FlagBtn({ id }: { id: number }) {
  const { has, toggle } = useFlaggedEpisodes();
  const { t } = useLocale();
  const flagged = has(id);
  return (
    <IconButton
      icon={Flag}
      size="sm"
      className="vw-flag-btn"
      pressed={flagged}
      label={t(flagged ? "Unflag episode" : "Flag for review")}
      onClick={() => toggle(id)}
    />
  );
}

function FlagAllBtn({ ids, label }: { ids: number[]; label?: string }) {
  const { addMany } = useFlaggedEpisodes();
  return (
    <T>
      {
        <button
          onClick={() => addMany(ids)}
          className="text-xs text-(--ds-text-secondary) hover:text-(--ds-text-primary) transition-colors flex items-center gap-1"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="10"
            height="10"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
            <line x1="4" y1="22" x2="4" y2="15" />
          </svg>
          {label ?? "Flag all"}
        </button>
      }
    </T>
  );
}

// ─── Lowest-Movement Episodes ────────────────────────────────────

function LowMovementSection({ episodes }: { episodes: LowMovementEpisode[] }) {
  if (episodes.length === 0) return null;
  const maxMovement = Math.max(...episodes.map((e) => e.totalMovement), 1e-10);

  return (
    <T>
      {
        <div className="bg-(--ds-surface-1) rounded-lg p-5 border border-(--ds-separator) space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-(--ds-text-primary)">
              <T>Lowest-Movement Episodes</T>
            </h3>
            <FlagAllBtn ids={episodes.map((e) => e.episodeIndex)} />
          </div>
          <p className="text-xs text-(--ds-text-secondary)">
            <T>
              Episodes with the lowest average action change per frame. Very low
              values may indicate the robot was standing still or the episode
              was recorded incorrectly.
            </T>
          </p>
          <div
            className="grid gap-2"
            style={{
              gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
            }}
          >
            {episodes.map((ep) => (
              <div
                key={ep.episodeIndex}
                className="bg-(--ds-surface-sunken) rounded-md px-3 py-2 flex items-center gap-3"
              >
                <FlagBtn id={ep.episodeIndex} />
                <span className="text-xs text-(--ds-text-secondary) font-medium shrink-0">
                  <T>ep </T>
                  <T>{ep.episodeIndex}</T>
                </span>
                <div className="flex-1 min-w-0">
                  <div className="h-1.5 bg-(--ds-surface-sunken) rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full"
                      style={{
                        width: `${Math.max(2, (ep.totalMovement / maxMovement) * 100)}%`,
                        background:
                          ep.totalMovement / maxMovement < 0.15
                            ? "var(--ds-danger)"
                            : ep.totalMovement / maxMovement < 0.4
                              ? "var(--ds-warning)"
                              : "var(--ds-success)",
                      }}
                    />
                  </div>
                </div>
                <span className="text-xs text-(--ds-text-secondary) tabular-nums shrink-0">
                  {ep.totalMovement.toFixed(2)}
                </span>
              </div>
            ))}
          </div>
        </div>
      }
    </T>
  );
}

// ─── Episode Length Filter ────────────────────────────────────────

function EpisodeLengthFilter({ episodes }: { episodes: EpisodeLengthInfo[] }) {
  const { addMany } = useFlaggedEpisodes();
  const globalMin = useMemo(
    () => Math.min(...episodes.map((e) => e.lengthSeconds)),
    [episodes],
  );
  const globalMax = useMemo(
    () => Math.max(...episodes.map((e) => e.lengthSeconds)),
    [episodes],
  );

  const [rangeMin, setRangeMin] = useState(globalMin);
  const [rangeMax, setRangeMax] = useState(globalMax);

  const outsideIds = useMemo(
    () =>
      episodes
        .filter((e) => e.lengthSeconds < rangeMin || e.lengthSeconds > rangeMax)
        .map((e) => e.episodeIndex)
        .sort((a, b) => a - b),
    [episodes, rangeMin, rangeMax],
  );

  const rangeChanged = rangeMin > globalMin || rangeMax < globalMax;
  const step =
    Math.max(0.01, Math.round((globalMax - globalMin) * 0.001 * 100) / 100) ||
    0.01;

  return (
    <T>
      {
        <div className="bg-(--ds-surface-1) rounded-lg p-5 border border-(--ds-separator) space-y-4">
          <h3 className="text-sm font-semibold text-(--ds-text-primary)">
            <T>Episode Length Filter</T>
          </h3>

          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-(--ds-text-secondary)">
              <span className="tabular-nums">{rangeMin.toFixed(1)}s</span>
              <span className="tabular-nums">{rangeMax.toFixed(1)}s</span>
            </div>
            <div className="relative h-5">
              <div className="absolute top-1/2 -translate-y-1/2 left-0 right-0 h-1 rounded bg-(--ds-surface-sunken)" />
              <div
                className="absolute top-1/2 -translate-y-1/2 h-1 rounded bg-(--ds-accent)"
                style={{
                  left: `${((rangeMin - globalMin) / (globalMax - globalMin || 1)) * 100}%`,
                  right: `${100 - ((rangeMax - globalMin) / (globalMax - globalMin || 1)) * 100}%`,
                }}
              />
              <input
                type="range"
                min={globalMin}
                max={globalMax}
                step={step}
                value={rangeMin}
                aria-label="Minimum episode length (s)"
                onChange={(e) =>
                  setRangeMin(Math.min(Number(e.target.value), rangeMax))
                }
                className="absolute inset-0 w-full appearance-none bg-transparent pointer-events-none [&::-webkit-slider-thumb]:pointer-events-auto [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-(--ds-surface-1) [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-(--ds-accent) [&::-webkit-slider-thumb]:cursor-pointer [&::-moz-range-thumb]:pointer-events-auto [&::-moz-range-thumb]:appearance-none [&::-moz-range-thumb]:w-3.5 [&::-moz-range-thumb]:h-3.5 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:bg-(--ds-surface-1) [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-(--ds-accent) [&::-moz-range-thumb]:cursor-pointer"
              />
              <input
                type="range"
                min={globalMin}
                max={globalMax}
                step={step}
                value={rangeMax}
                aria-label="Maximum episode length (s)"
                onChange={(e) =>
                  setRangeMax(Math.max(Number(e.target.value), rangeMin))
                }
                className="absolute inset-0 w-full appearance-none bg-transparent pointer-events-none [&::-webkit-slider-thumb]:pointer-events-auto [&::-webkit-slider-thumb]:appearance-none [&::-webkit-slider-thumb]:w-3.5 [&::-webkit-slider-thumb]:h-3.5 [&::-webkit-slider-thumb]:rounded-full [&::-webkit-slider-thumb]:bg-(--ds-surface-1) [&::-webkit-slider-thumb]:border-2 [&::-webkit-slider-thumb]:border-(--ds-accent) [&::-webkit-slider-thumb]:cursor-pointer [&::-moz-range-thumb]:pointer-events-auto [&::-moz-range-thumb]:appearance-none [&::-moz-range-thumb]:w-3.5 [&::-moz-range-thumb]:h-3.5 [&::-moz-range-thumb]:rounded-full [&::-moz-range-thumb]:bg-(--ds-surface-1) [&::-moz-range-thumb]:border-2 [&::-moz-range-thumb]:border-(--ds-accent) [&::-moz-range-thumb]:cursor-pointer"
              />
            </div>
          </div>

          {rangeChanged && (
            <div className="flex items-center justify-between">
              <span className="text-xs text-(--ds-text-secondary)">
                <T>{outsideIds.length}</T>
                <T> episode</T>
                <T>{outsideIds.length !== 1 ? "s" : ""}</T>
                <T> </T>
                <T>outside range</T>
              </span>
              {outsideIds.length > 0 && (
                <button
                  onClick={() => addMany(outsideIds)}
                  className="text-xs bg-(--ds-surface-selected) text-(--ds-text-primary) border border-(--ds-accent) rounded px-2 py-1 hover:bg-(--ds-surface-selected) transition-colors"
                >
                  <T>Flag </T>
                  <T>{outsideIds.length}</T>
                  <T> outside range</T>
                </button>
              )}
            </div>
          )}
        </div>
      }
    </T>
  );
}

// ─── Main Filtering Panel ────────────────────────────────────────

interface FilteringPanelProps {
  repoId: string;
  crossEpisodeData: CrossEpisodeVarianceData | null;
  crossEpisodeLoading: boolean;
  episodeLengthStats: EpisodeLengthStats | null;
  flatChartData: Record<string, number>[];
  onViewFlaggedEpisodes?: () => void;
}

function FlaggedIdsCopyBar({
  repoId,
  onViewEpisodes,
}: {
  repoId: string;
  onViewEpisodes?: () => void;
}) {
  const { flagged, count, clear } = useFlaggedEpisodes();
  const [copied, setCopied] = useState(false);

  const ids = useMemo(() => [...flagged].sort((a, b) => a - b), [flagged]);
  const idStr = ids.join(", ");

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(idStr);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, [idStr]);

  if (count === 0) return null;

  return (
    <T>
      {
        <div className="bg-(--ds-surface-1) rounded-lg p-4 border border-(--ds-border-control) space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-(--ds-text-primary)">
              <T>Flagged Episodes</T>
              <span className="text-xs text-(--ds-text-secondary) ml-2 font-normal">
                (<T>{count}</T>)
              </span>
            </h3>
            <div className="flex items-center gap-2">
              <button
                onClick={handleCopy}
                className="text-xs text-(--ds-text-secondary) hover:text-(--ds-text-primary) transition-colors flex items-center gap-1"
                title="Copy IDs"
              >
                <T>
                  {copied ? (
                    <svg
                      xmlns="http://www.w3.org/2000/svg"
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      className="text-(--ds-success)"
                    >
                      <polyline points="20 6 9 17 4 12" />
                    </svg>
                  ) : (
                    <svg
                      xmlns="http://www.w3.org/2000/svg"
                      width="12"
                      height="12"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                    >
                      <rect x="9" y="9" width="13" height="13" rx="2" />
                      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                    </svg>
                  )}
                </T>
                <T>Copy</T>
              </button>
              <button
                onClick={clear}
                className="text-xs text-(--ds-text-secondary) hover:text-(--ds-danger) transition-colors"
              >
                <T>Clear</T>
              </button>
            </div>
          </div>
          <p className="text-xs text-(--ds-text-secondary) tabular-nums leading-relaxed max-h-20 overflow-y-auto">
            <T>{idStr}</T>
          </p>
          {onViewEpisodes && (
            <button
              onClick={onViewEpisodes}
              className="w-full text-xs py-1.5 rounded bg-(--ds-surface-2) hover:bg-(--ds-surface-sunken) text-(--ds-text-secondary) hover:text-(--ds-text-primary) transition-colors flex items-center justify-center gap-1.5"
            >
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" />
                <line x1="4" y1="22" x2="4" y2="15" />
              </svg>
              <T>View flagged episodes</T>
            </button>
          )}
          <div className="bg-(--ds-surface-sunken) rounded-md px-3 py-2 border border-(--ds-separator) space-y-2.5">
            <p className="text-xs text-(--ds-text-secondary)">
              <a
                href="https://github.com/huggingface/lerobot"
                target="_blank"
                rel="noopener noreferrer"
                className="text-(--ds-text-primary) underline"
              >
                <T>LeRobot CLI</T>
              </a>
              <T> </T>
              <T>— delete flagged episodes:</T>
            </p>
            <pre className="text-xs text-(--ds-text-secondary) bg-(--ds-bg) rounded px-2 py-1.5 overflow-x-auto select-all">
              <T>{`# Delete episodes (modifies original dataset)\nlerobot-edit-dataset \\\n    --repo_id ${repoId} \\\n    --operation.type delete_episodes \\\n    --operation.episode_indices "[${ids.join(", ")}]"`}</T>
            </pre>
            <pre className="text-xs text-(--ds-text-secondary) bg-(--ds-bg) rounded px-2 py-1.5 overflow-x-auto select-all">
              <T>{`# Delete episodes and save to a new dataset (preserves original)\nlerobot-edit-dataset \\\n    --repo_id ${repoId} \\\n    --new_repo_id ${repoId}_filtered \\\n    --operation.type delete_episodes \\\n    --operation.episode_indices "[${ids.join(", ")}]"`}</T>
            </pre>
          </div>
        </div>
      }
    </T>
  );
}

function FilteringPanel({
  repoId,
  crossEpisodeData,
  crossEpisodeLoading,
  episodeLengthStats,
  flatChartData,
  onViewFlaggedEpisodes,
}: FilteringPanelProps) {
  return (
    <T>
      {
        <div className="max-w-5xl mx-auto py-6 space-y-8">
          <div>
            <h2 className="text-xl font-semibold text-(--ds-text-primary)">
              <T>Filtering</T>
            </h2>
            <p className="text-sm text-(--ds-text-secondary) mt-1">
              <T>
                Identify and flag problematic episodes for removal. Flagged
                episodes appear in the sidebar and can be exported as a CLI
                command.
              </T>
            </p>
          </div>

          <FlaggedIdsCopyBar
            repoId={repoId}
            onViewEpisodes={onViewFlaggedEpisodes}
          />

          {episodeLengthStats?.allEpisodeLengths && (
            <EpisodeLengthFilter
              episodes={episodeLengthStats.allEpisodeLengths}
            />
          )}

          {crossEpisodeLoading && (
            <div className="bg-(--ds-surface-1) rounded-lg p-5 border border-(--ds-separator)">
              <div className="flex items-center gap-2 text-(--ds-text-secondary) text-sm py-4 justify-center">
                <Icon icon={LoaderCircle} className="ds-spin" />
                <T>Loading cross-episode data…</T>
              </div>
            </div>
          )}

          {crossEpisodeData?.lowMovementEpisodes && (
            <LowMovementSection
              episodes={crossEpisodeData.lowMovementEpisodes}
            />
          )}

          <FullscreenWrapper>
            <ActionVelocitySection
              data={flatChartData}
              agg={crossEpisodeData?.aggVelocity}
              numEpisodes={crossEpisodeData?.numEpisodes}
              jerkyEpisodes={crossEpisodeData?.jerkyEpisodes}
            />
          </FullscreenWrapper>
        </div>
      }
    </T>
  );
}

export default FilteringPanel;
