// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { Spinner, Tooltip } from "@/components/ds";
import { T, useLocale } from "@/components/levi-locale";
import { AnalysisCard, fill } from "@/components/viewer/analysis-ui";
import { roundTo2 } from "@/components/viewer/time-format";

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
  const { t } = useLocale();
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
  const topPad = 18;
  const svgWidth = data.length * (barWidth + gap);
  const labelStep = Math.max(1, Math.ceil(data.length / 10));

  return (
    <T>
      {
        <div className="vw-a-histogram-wrap">
          <svg
            width={svgWidth}
            height={topPad + chartHeight + labelHeight}
            className="vw-a-histogram"
            role="img"
            aria-label={t("Episode length distribution histogram")}
          >
            {data.map((bin, i) => {
              const barH = Math.max(1, (bin.count / maxCount) * chartHeight);
              const x = i * (barWidth + gap);
              const y = topPad + chartHeight - barH;
              return (
                <g key={i}>
                  <title>
                    {`${bin.binLabel}: ${bin.count} ${t(bin.count === 1 ? "episode" : "episodes")}`}
                  </title>
                  <rect
                    x={x}
                    y={y}
                    width={barWidth}
                    height={barH}
                    className="vw-a-bar"
                    fill="var(--dv-1)"
                    rx={Math.min(2, barWidth / 4)}
                  />
                  {bin.count > 0 && barWidth >= 14 && (
                    <text
                      x={x + barWidth / 2}
                      y={y - 3}
                      textAnchor="middle"
                      className="vw-a-svgtext"
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
                  y={topPad + chartHeight + 16}
                  textAnchor="middle"
                  className="vw-a-svgtext"
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

/** Label and value tiles: the labels are terms, the values numbers. */
function Metrics({
  items,
}: {
  items: { label: string; value: string | number }[];
}) {
  const { t } = useLocale();
  return (
    <dl className="vw-metrics">
      {items.map((item) => (
        <div key={item.label}>
          <dt>{t(item.label)}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function StatsPanel({
  datasetInfo,
  taskCount,
  episodeLengthStats,
  loading,
}: StatsPanelProps) {
  const els = episodeLengthStats;
  const { t } = useLocale();

  return (
    <div className="vw-a-view">
      <div>
        <h2 className="vw-a-section-title">
          {t("Dataset Statistics:")}{" "}
          <span className="vw-muted">{datasetInfo.repoId}</span>
        </h2>
      </div>

      <Metrics
        items={[
          { label: "Robot Type", value: datasetInfo.robot_type ?? "unknown" },
          { label: "Dataset Version", value: datasetInfo.codebase_version },
          { label: "Tasks", value: taskCount ?? datasetInfo.total_tasks },
        ]}
      />
      <Metrics
        items={[
          {
            label: "Total Frames",
            value: datasetInfo.total_frames.toLocaleString(),
          },
          {
            label: "Total Episodes",
            value: datasetInfo.total_episodes.toLocaleString(),
          },
          { label: "FPS", value: roundTo2(datasetInfo.fps) },
          {
            label: "Total Recording Time",
            value: formatTotalTime(datasetInfo.total_frames, datasetInfo.fps),
          },
        ]}
      />

      {datasetInfo.cameras.length > 0 && (
        <AnalysisCard title="Camera Resolutions">
          <div className="vw-a-grid vw-a-grid--wide">
            {datasetInfo.cameras.map((cam: CameraInfo) => (
              <div key={cam.name} className="vw-a-tile">
                <p className="vw-a-tile__name">
                  <Tooltip content={cam.name}>
                    <span tabIndex={0}>{cam.name}</span>
                  </Tooltip>
                </p>
                <p className="vw-a-callout__value">
                  {cam.width}×{cam.height}
                </p>
              </div>
            ))}
          </div>
        </AnalysisCard>
      )}

      {loading && (
        <div role="status">
          <Spinner label={t("Computing episode statistics…")} showLabel />
        </div>
      )}

      {els && (
        <>
          <AnalysisCard title="Episode Lengths">
            <Metrics
              items={[
                {
                  label: "Shortest",
                  value: `${els.shortestEpisodes[0]?.lengthSeconds ?? "–"}s`,
                },
                {
                  label: "Longest",
                  value: `${els.longestEpisodes[0]?.lengthSeconds ?? "–"}s`,
                },
                { label: "Mean", value: `${els.meanEpisodeLength}s` },
                { label: "Median", value: `${els.medianEpisodeLength}s` },
                { label: "Std Dev", value: `${els.stdEpisodeLength}s` },
              ]}
            />
          </AnalysisCard>

          {els.episodeLengthHistogram.length > 0 && (
            <AnalysisCard
              title="Episode Length Distribution"
              meta={fill(
                t(
                  els.episodeLengthHistogram.length !== 1
                    ? "{n} bins"
                    : "{n} bin",
                ),
                { n: els.episodeLengthHistogram.length },
              )}
            >
              <EpisodeLengthHistogram data={els.episodeLengthHistogram} />
            </AnalysisCard>
          )}
        </>
      )}
    </div>
  );
}

export default StatsPanel;
