// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { Icon } from "@/components/ds";
import { LoaderCircle } from "lucide-react";
import { T } from "@/components/levi-locale";

import type {
  DatasetDisplayInfo,
  EpisodeLengthStats,
  CameraInfo,
} from "@/app/[org]/[dataset]/[episode]/fetch-data";

interface StatsPanelProps {
  datasetInfo: DatasetDisplayInfo;
  /** Actual unique task count from task_index metadata, when available. */
  taskCount?: number;
  episodeLengthStats: EpisodeLengthStats | null;
  loading: boolean;
}

function formatTotalTime(totalFrames: number, fps: number): string {
  const totalSec = totalFrames / fps;
  const hours = Math.floor(totalSec / 3600);
  const minutes = Math.floor((totalSec % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

/** SVG bar chart for the episode-length histogram */
function EpisodeLengthHistogram({
  data,
}: {
  data: { binLabel: string; count: number }[];
}) {
  if (data.length === 0) return null;
  const maxCount = Math.max(...data.map((d) => d.count));
  if (maxCount === 0) return null;

  const totalWidth = 560;
  const gap = Math.max(1, Math.min(3, Math.floor(60 / data.length)));
  const barWidth = Math.max(
    4,
    Math.floor((totalWidth - gap * data.length) / data.length),
  );
  const chartHeight = 150;
  const labelHeight = 30;
  const topPad = 16;
  const svgWidth = data.length * (barWidth + gap);
  const labelStep = Math.max(1, Math.ceil(data.length / 10));

  return (
    <T>
      {
        <div className="overflow-x-auto">
          <svg
            width={svgWidth}
            height={topPad + chartHeight + labelHeight}
            className="block"
            aria-label="Episode length distribution histogram"
          >
            {data.map((bin, i) => {
              const barH = Math.max(1, (bin.count / maxCount) * chartHeight);
              const x = i * (barWidth + gap);
              const y = topPad + chartHeight - barH;
              return (
                <g key={i}>
                  <title>
                    <T>{`${bin.binLabel}: ${bin.count} episode${bin.count !== 1 ? "s" : ""}`}</T>
                  </title>
                  <rect
                    x={x}
                    y={y}
                    width={barWidth}
                    height={barH}
                    className="fill-(--dv-1) hover:opacity-80 transition-opacity"
                    rx={Math.min(2, barWidth / 4)}
                  />
                  {bin.count > 0 && barWidth >= 8 && (
                    <text
                      x={x + barWidth / 2}
                      y={y - 3}
                      textAnchor="middle"
                      className="fill-(--ds-text-secondary)"
                      fontSize={Math.min(10, barWidth - 1)}
                    >
                      <T>{bin.count}</T>
                    </text>
                  )}
                </g>
              );
            })}
            {data.map((bin, idx) => {
              const isFirst = idx === 0;
              const isLast = idx === data.length - 1;
              if (!isFirst && !isLast && idx % labelStep !== 0) return null;
              const label = bin.binLabel.split("–")[0];
              return (
                <text
                  key={idx}
                  x={idx * (barWidth + gap) + barWidth / 2}
                  y={topPad + chartHeight + 14}
                  textAnchor="middle"
                  className="fill-(--ds-text-secondary)"
                  fontSize={9}
                >
                  <T>{label}</T>s
                </text>
              );
            })}
          </svg>
        </div>
      }
    </T>
  );
}

function Card({ label, value }: { label: string; value: string | number }) {
  return (
    <T>
      {
        <div className="bg-(--ds-surface-1) rounded-lg p-4 border border-(--ds-separator)">
          <p className="text-xs text-(--ds-text-secondary) ">
            <T>{label}</T>
          </p>
          <p className="text-xl font-semibold tabular-nums mt-1">
            <T>{value}</T>
          </p>
        </div>
      }
    </T>
  );
}

function StatsPanel({
  datasetInfo,
  taskCount,
  episodeLengthStats,
  loading,
}: StatsPanelProps) {
  const els = episodeLengthStats;

  return (
    <T>
      {
        <div className="max-w-4xl mx-auto py-6 space-y-8">
          <div>
            <h2 className="text-xl text-(--ds-text-primary)">
              <span className="font-semibold">
                <T>Dataset Statistics:</T>
              </span>
              <T> </T>
              <span className="font-normal text-(--ds-text-secondary)">
                <T>{datasetInfo.repoId}</T>
              </span>
            </h2>
          </div>

          {/* Overview cards */}
          <div className="grid grid-cols-3 gap-4">
            <Card
              label="Robot Type"
              value={datasetInfo.robot_type ?? "unknown"}
            />
            <Card
              label="Dataset Version"
              value={datasetInfo.codebase_version}
            />
            <Card label="Tasks" value={taskCount ?? datasetInfo.total_tasks} />
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <Card
              label="Total Frames"
              value={datasetInfo.total_frames.toLocaleString()}
            />
            <Card
              label="Total Episodes"
              value={datasetInfo.total_episodes.toLocaleString()}
            />
            <Card label="FPS" value={datasetInfo.fps} />
            <Card
              label="Total Recording Time"
              value={formatTotalTime(datasetInfo.total_frames, datasetInfo.fps)}
            />
          </div>

          {/* Camera resolutions */}
          {datasetInfo.cameras.length > 0 && (
            <div className="bg-(--ds-surface-1) rounded-lg p-5 border border-(--ds-separator)">
              <h3 className="text-sm font-semibold text-(--ds-text-primary) mb-3">
                <T>Camera Resolutions</T>
              </h3>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                {datasetInfo.cameras.map((cam: CameraInfo) => (
                  <div
                    key={cam.name}
                    className="bg-(--ds-surface-sunken) rounded-md p-3"
                  >
                    <p
                      className="text-xs text-(--ds-text-secondary) mb-1 truncate"
                      title={cam.name}
                    >
                      <T>{cam.name}</T>
                    </p>
                    <p className="text-base font-semibold tabular-nums">
                      <T>{cam.width}</T>×<T>{cam.height}</T>
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Loading spinner for async stats */}
          {loading && (
            <div className="flex items-center gap-2 text-(--ds-text-secondary) text-sm py-4">
              <Icon icon={LoaderCircle} className="ds-spin" />
              <T>Computing episode statistics…</T>
            </div>
          )}

          {/* Episode length section */}
          {els && (
            <>
              <div className="bg-(--ds-surface-1) rounded-lg p-5 border border-(--ds-separator)">
                <h3 className="text-sm font-semibold text-(--ds-text-primary) mb-4">
                  <T>Episode Lengths</T>
                </h3>
                <div className="grid grid-cols-3 md:grid-cols-5 gap-4 mb-4">
                  <Card
                    label="Shortest"
                    value={`${els.shortestEpisodes[0]?.lengthSeconds ?? "–"}s`}
                  />
                  <Card
                    label="Longest"
                    value={`${els.longestEpisodes[0]?.lengthSeconds ?? "–"}s`}
                  />
                  <Card label="Mean" value={`${els.meanEpisodeLength}s`} />
                  <Card label="Median" value={`${els.medianEpisodeLength}s`} />
                  <Card label="Std Dev" value={`${els.stdEpisodeLength}s`} />
                </div>
              </div>

              {els.episodeLengthHistogram.length > 0 && (
                <div className="bg-(--ds-surface-1) rounded-lg p-5 border border-(--ds-separator)">
                  <h3 className="text-sm font-semibold text-(--ds-text-primary) mb-4">
                    <T>Episode Length Distribution</T>
                    <span className="text-xs text-(--ds-text-secondary) ml-2 font-normal">
                      <T>{els.episodeLengthHistogram.length}</T>
                      <T>
                        {els.episodeLengthHistogram.length !== 1
                          ? " bins"
                          : " bin"}
                      </T>
                    </span>
                  </h3>
                  <EpisodeLengthHistogram data={els.episodeLengthHistogram} />
                </div>
              )}
            </>
          )}
        </div>
      }
    </T>
  );
}

export default StatsPanel;
