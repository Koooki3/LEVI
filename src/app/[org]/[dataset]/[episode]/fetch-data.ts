// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
import {
  DatasetMetadata,
  fetchParquetFile,
  formatStringWithVars,
  readParquetAsObjects,
} from "@/utils/parquetUtils";
import { pick } from "@/utils/pick";
import { authHeaders } from "@/utils/auth";
import {
  getDatasetVersionAndInfo,
  buildVersionedUrl,
  getDatasetStats,
  isDatasetV3,
  normalizeDatasetVersion,
} from "@/utils/versionUtils";
import { PADDING, CHART_CONFIG, EXCLUDED_COLUMNS } from "@/utils/constants";
import {
  processChartDataGroups,
  groupRowBySuffix,
} from "@/utils/dataProcessing";
import {
  buildV3VideoPath,
  buildV3DataPath,
  buildV3EpisodesMetadataPath,
} from "@/utils/stringFormatting";
import { bigIntToNumber } from "@/utils/typeGuards";
import { extractLanguageInstructions } from "@/utils/languageInstructions";
import {
  isGrayscaleShape,
  depthColormapRange,
  depthEncodingFromFeature,
} from "@/utils/colormaps";
import type { VideoInfo, AdjacentEpisodeVideos } from "@/types";

const SERIES_NAME_DELIMITER = CHART_CONFIG.SERIES_NAME_DELIMITER;

export type CameraInfo = { name: string; width: number; height: number };

export type DatasetDisplayInfo = {
  repoId: string;
  total_frames: number;
  total_episodes: number;
  fps: number;
  robot_type: string | null;
  codebase_version: string;
  total_tasks: number;
  dataset_size_mb: number;
  cameras: CameraInfo[];
};

export type ChartRow = Record<string, number | Record<string, number>>;

export type ColumnMinMax = {
  column: string;
  min: number;
  max: number;
};

export type EpisodeLengthInfo = {
  episodeIndex: number;
  lengthSeconds: number;
  frames: number;
};

export type EpisodeLengthStats = {
  shortestEpisodes: EpisodeLengthInfo[];
  longestEpisodes: EpisodeLengthInfo[];
  allEpisodeLengths: EpisodeLengthInfo[];
  meanEpisodeLength: number;
  medianEpisodeLength: number;
  stdEpisodeLength: number;
  episodeLengthHistogram: { binLabel: string; count: number }[];
};

export type EpisodeFrameInfo = {
  episodeIndex: number;
  videoUrl: string;
  firstFrameTime: number;
  lastFrameTime: number | null; // null = seek to video.duration on client
};

export type EpisodeFramesData = {
  cameras: string[];
  framesByCamera: Record<string, EpisodeFrameInfo[]>;
};

/**
 * Task ↔ episode mapping for the whole dataset. Multi-task captures (lerobot
 * allows several task strings per episode) use this to filter the episode list
 * and to scope Action Insights to one task.
 */
export type DatasetTaskIndex = {
  /** Unique task strings, ordered by `task_index` when the dataset ships one. */
  tasks: string[];
  /** `episode_index` → the task strings recorded for that episode. */
  episodeTasks: Record<number, string[]>;
};

export type EpisodeData = {
  datasetInfo: DatasetDisplayInfo;
  episodeId: number;
  videosInfo: VideoInfo[];
  chartDataGroups: ChartRow[][];
  flatChartData: Record<string, number>[];
  episodes: number[];
  ignoredColumns: string[];
  duration: number;
  task?: string;
  /**
   * v3.1 language atoms read from `language_persistent` and `language_events`
   * (lerobot#3467). Empty when the dataset hasn't been annotated yet.
   * The annotations panel uses this to seed its editor state when no
   * annotation backend is running.
   */
  languageAtoms?: import("@/types/language.types").LanguageAtom[];
  /**
   * Sorted source-frame timestamps from the data parquet, in seconds. Used by
   * the annotations editor to snap atom timestamps to exact frame times —
   * avoiding sub-frame drift from a throttled `currentTime` and keeping
   * events compatible with the writer in lerobot#3471.
   */
  frameTimestamps?: number[];
};

type EpisodeMetadataV3 = {
  episode_index: number;
  data_chunk_index: number;
  data_file_index: number;
  dataset_from_index: number;
  dataset_to_index: number;
  video_chunk_index: number;
  video_file_index: number;
  video_from_timestamp: number;
  video_to_timestamp: number;
  length: number;
  tasks?: string[];
  task_index?: number;
  [key: string]: string | number | string[] | undefined;
};

type ColumnDef = {
  key: string;
  value: string[];
};

function parsePositiveIntEnv(
  value: string | undefined,
  fallback: number,
  min = 1,
): number {
  const parsed = Number.parseInt(value ?? "", 10);
  return Number.isFinite(parsed) && parsed >= min ? parsed : fallback;
}

const MAX_EPISODE_POINTS = parsePositiveIntEnv(
  process.env.MAX_EPISODE_POINTS,
  4000,
  100,
);
const MAX_FRAMES_OVERVIEW_EPISODES = parsePositiveIntEnv(
  process.env.MAX_FRAMES_OVERVIEW_EPISODES,
  3000,
  100,
);
/**
 * Episodes analysed when the Action Insights panel first opens. A starting
 * point, not a ceiling — the panel can widen the sample or ask for every
 * episode in scope (bounded only by `CROSS_EPISODE_SAMPLE_CEILING`).
 */
const DEFAULT_CROSS_EPISODE_SAMPLE = parsePositiveIntEnv(
  process.env.MAX_CROSS_EPISODE_SAMPLE,
  120,
  10,
);
/**
 * Hard upper bound for a single analysis run. This exists to stop a runaway
 * request on a very large capture, not to cap ordinary reviews — a full-dataset
 * pass over a few thousand episodes stays well under it.
 */
const CROSS_EPISODE_SAMPLE_CEILING = parsePositiveIntEnv(
  process.env.MAX_CROSS_EPISODE_TOTAL,
  20000,
  10,
);
/** Parallel parquet reads while collecting episodes for one analysis run. */
const CROSS_EPISODE_FETCH_CONCURRENCY = parsePositiveIntEnv(
  process.env.CROSS_EPISODE_FETCH_CONCURRENCY,
  12,
  1,
);
const MAX_CROSS_EPISODE_FRAMES_PER_EPISODE = parsePositiveIntEnv(
  process.env.MAX_CROSS_EPISODE_FRAMES_PER_EPISODE,
  2500,
  100,
);

export const CROSS_EPISODE_DEFAULTS = {
  /** Initial sample size offered by the Action Insights scope controls. */
  sampleSize: DEFAULT_CROSS_EPISODE_SAMPLE,
  /** Largest sample a single run will accept. */
  ceiling: CROSS_EPISODE_SAMPLE_CEILING,
} as const;
const PROGRESS_PARQUET_CANDIDATES = [
  "sarm_progress.parquet",
  "srm_progress.parquet",
] as const;
const PREFERRED_PROGRESS_COLUMNS = [
  "progress_sparse",
  "progress_dense",
  "progress",
] as const;

function evenlySampleIndices(length: number, target: number): number[] {
  if (length <= 0) return [];
  if (target >= length) return Array.from({ length }, (_, i) => i);
  if (target <= 1) return [0];

  const sampled = new Set<number>();
  for (let i = 0; i < target; i++) {
    sampled.add(Math.round((i * (length - 1)) / (target - 1)));
  }

  // Fill potential gaps caused by rounding collisions.
  if (sampled.size < target) {
    for (let i = 0; i < length && sampled.size < target; i++) {
      sampled.add(i);
    }
  }

  return Array.from(sampled).sort((a, b) => a - b);
}

function evenlySampleArray<T>(items: T[], maxCount: number): T[] {
  if (items.length <= maxCount) return items;
  return evenlySampleIndices(items.length, maxCount).map((idx) => items[idx]);
}

/**
 * `Promise.all(items.map(...))` with a ceiling on in-flight work. A
 * full-dataset insights run touches one parquet file per episode, so firing
 * every request at once would queue thousands of fetches and hold every
 * response in memory at the same time.
 */
async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  worker: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results = new Array<R>(items.length);
  let cursor = 0;
  const runnerCount = Math.max(1, Math.min(limit, items.length));
  await Promise.all(
    Array.from({ length: runnerCount }, async () => {
      for (;;) {
        const index = cursor++;
        if (index >= items.length) return;
        results[index] = await worker(items[index], index);
      }
    }),
  );
  return results;
}

const CHART_NUMERIC_DTYPES = new Set([
  "float16",
  "float32",
  "float64",
  "int8",
  "int16",
  "int32",
  "int64",
  "uint8",
  "uint16",
  "uint32",
  "uint64",
  "bool",
  "boolean",
]);

function isChartNumericDType(dtype: unknown): boolean {
  return (
    typeof dtype === "string" && CHART_NUMERIC_DTYPES.has(dtype.toLowerCase())
  );
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "bigint") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  if (typeof value === "string" && value.trim().length > 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function buildProgressSeriesKey(progressColumn: string): string {
  if (progressColumn === "progress_sparse") {
    return `progress${SERIES_NAME_DELIMITER}sparse`;
  }
  if (progressColumn === "progress_dense") {
    return `progress${SERIES_NAME_DELIMITER}dense`;
  }
  return "progress";
}

function pickProgressColumn(rows: Record<string, unknown>[]): string | null {
  if (rows.length === 0) return null;

  const columnNames = Object.keys(rows[0] ?? {});
  const preferred = PREFERRED_PROGRESS_COLUMNS.filter((column) =>
    columnNames.includes(column),
  );
  const preferredSet = new Set<string>(preferred);
  const additionalProgressColumns = columnNames
    .filter(
      (column) => column.startsWith("progress_") && !preferredSet.has(column),
    )
    .sort();
  const candidates = [...preferred, ...additionalProgressColumns];

  for (const column of candidates) {
    const hasFiniteValue = rows.some(
      (row) => toFiniteNumber(row[column]) !== null,
    );
    if (hasFiniteValue) {
      return column;
    }
  }

  return null;
}

// Returns a builder that, given the episode's final duration, produces the
// scaled progress chart group. Splitting "fetch the parquet" from "scale by
// duration" lets the caller kick off the fetch in parallel with the main
// episode-data fetch instead of waiting on a sequential await.
async function loadEpisodeProgressGroup(
  repoId: string,
  version: string,
  episodeId: number,
): Promise<((episodeDuration: number) => ChartRow[]) | null> {
  for (const progressPath of PROGRESS_PARQUET_CANDIDATES) {
    const progressUrl = buildVersionedUrl(repoId, version, progressPath);
    try {
      const progressBuffer = await fetchParquetFile(progressUrl);
      const progressRows = await readParquetAsObjects(progressBuffer, []);
      if (progressRows.length === 0) continue;

      const hasEpisodeIndex = progressRows.some(
        (row) => toFiniteNumber(row["episode_index"]) !== null,
      );
      const targetRows = hasEpisodeIndex
        ? progressRows.filter(
            (row) => toFiniteNumber(row["episode_index"]) === episodeId,
          )
        : progressRows;
      if (targetRows.length === 0) continue;

      const progressColumn = pickProgressColumn(targetRows);
      if (!progressColumn) continue;

      const orderedPoints: Array<{ order: number; progress: number }> = [];
      for (let i = 0; i < targetRows.length; i++) {
        const row = targetRows[i];
        const progressValue = toFiniteNumber(row[progressColumn]);
        if (progressValue === null) continue;

        const order =
          toFiniteNumber(row["index"]) ??
          toFiniteNumber(row["frame_index"]) ??
          i;
        orderedPoints.push({ order, progress: progressValue });
      }

      if (orderedPoints.length === 0) continue;
      orderedPoints.sort((a, b) => a.order - b.order);

      const sampledPoints = evenlySampleArray(
        orderedPoints,
        MAX_EPISODE_POINTS,
      );
      const progressKey = buildProgressSeriesKey(progressColumn);
      const denominator = Math.max(sampledPoints.length - 1, 1);

      return (episodeDuration: number) => {
        const duration = Math.max(episodeDuration, 0);
        return sampledPoints.map((point, idx) => ({
          timestamp:
            sampledPoints.length === 1 ? 0 : (idx / denominator) * duration,
          [progressKey]: point.progress,
        }));
      };
    } catch {
      // Optional file: ignore and try next candidate.
    }
  }

  return null;
}

function buildSampledEpisodeSet(
  totalEpisodes: number,
  maxEpisodes: number,
): Set<number> | null {
  if (totalEpisodes <= maxEpisodes) return null;
  return new Set(evenlySampleIndices(totalEpisodes, maxEpisodes));
}

export async function getEpisodeData(
  org: string,
  dataset: string,
  episodeId: number,
): Promise<EpisodeData> {
  const repoId = `${org}/${dataset}`;
  try {
    console.time(`[perf] getDatasetVersionAndInfo`);
    const { version, info: rawInfo } = await getDatasetVersionAndInfo(repoId);
    console.timeEnd(`[perf] getDatasetVersionAndInfo`);
    const info = rawInfo as unknown as DatasetMetadata;

    const normalizedVersion = normalizeDatasetVersion(version);
    if (!normalizedVersion)
      throw new Error("Unsupported dataset version: " + version);
    if (
      !Number.isInteger(info.total_episodes) ||
      episodeId < 0 ||
      episodeId >= info.total_episodes
    ) {
      throw new Error("Episode " + episodeId + " is outside the dataset range");
    }
    if (!Number.isFinite(info.fps) || info.fps <= 0) {
      throw new Error("Dataset metadata has an invalid FPS");
    }
    const hasVideoFeature = Object.values(info.features).some(
      (feature) => feature.dtype === "video",
    );
    if (
      !hasVideoFeature ||
      (!isDatasetV3(normalizedVersion) && info.video_path === null)
    ) {
      throw new Error(
        "Only videos datasets are supported in this visualizer.\nPlease use Rerun visualizer for images datasets.",
      );
    }

    // Run the main episode-data fetch and the optional progress parquet
    // in parallel. Previously they ran serially: the progress fetch was
    // gated on result.duration even though it only used duration to scale
    // timestamps at the end. Now loadEpisodeProgressGroup returns a
    // builder we apply once both promises settle.
    // Vercel rule: async-parallel.
    // Only single-channel feeds are recolored, so only they need q10/q90 from
    // stats.json — skip the extra fetch entirely for ordinary RGB datasets.
    const hasGrayscaleFeed = Object.values(rawInfo.features).some(
      (f) => f.dtype === "video" && isGrayscaleShape(f.shape),
    );

    console.time(`[perf] getEpisodeData (${version})`);
    const [result, progressBuilder, stats] = await Promise.all([
      isDatasetV3(normalizedVersion)
        ? getEpisodeDataV3(repoId, version, info, episodeId)
        : getEpisodeDataV2(repoId, version, info, episodeId),
      loadEpisodeProgressGroup(repoId, version, episodeId),
      hasGrayscaleFeed ? getDatasetStats(repoId) : Promise.resolve(null),
    ]);
    console.timeEnd(`[perf] getEpisodeData (${version})`);

    // Stretch each grayscale feed's colormap to its q10/q90 band so outliers
    // don't wash out the visualization. Single-channel depth feeds map the
    // quantiles through their quantization params; plain grayscale feeds use
    // the already-normalized quantiles directly.
    if (stats) {
      for (const v of result.videosInfo) {
        if (!v.isGrayscale) continue;
        const encoding = depthEncodingFromFeature(rawInfo.features[v.filename]);
        const range = depthColormapRange(stats[v.filename], encoding);
        if (range) v.colormapRange = range;
      }
    }

    // Extract camera resolutions from features
    const cameras: CameraInfo[] = Object.entries(rawInfo.features)
      .filter(([, f]) => f.dtype === "video" && f.shape.length >= 2)
      .map(([name, f]) => ({ name, height: f.shape[0], width: f.shape[1] }));

    result.datasetInfo = {
      ...result.datasetInfo,
      robot_type: rawInfo.robot_type ?? null,
      codebase_version: rawInfo.codebase_version,
      total_tasks: rawInfo.total_tasks ?? 0,
      dataset_size_mb:
        Math.round(
          ((rawInfo.data_files_size_in_mb ?? 0) +
            (rawInfo.video_files_size_in_mb ?? 0)) *
            10,
        ) / 10,
      cameras,
    };

    if (progressBuilder) {
      const progressGroup = progressBuilder(result.duration);
      if (progressGroup.length > 0) {
        result.chartDataGroups = [...result.chartDataGroups, progressGroup];
      }
    }

    return result;
  } catch (err) {
    console.error("Error loading episode data:", err);
    throw err;
  }
}

export async function getAdjacentEpisodesVideoInfo(
  org: string,
  dataset: string,
  currentEpisodeId: number,
  radius: number = 2,
): Promise<AdjacentEpisodeVideos[]> {
  const repoId = `${org}/${dataset}`;
  try {
    const { version, info: rawInfo } = await getDatasetVersionAndInfo(repoId);
    const info = rawInfo as unknown as DatasetMetadata;

    const totalEpisodes = info.total_episodes;
    const adjacentVideos: AdjacentEpisodeVideos[] = [];

    // Calculate adjacent episode IDs
    for (let offset = -radius; offset <= radius; offset++) {
      if (offset === 0) continue; // Skip current episode

      const episodeId = currentEpisodeId + offset;
      if (episodeId >= 0 && episodeId < totalEpisodes) {
        try {
          let videosInfo: VideoInfo[] = [];

          if (isDatasetV3(version)) {
            const episodeMetadata = await loadEpisodeMetadataV3Simple(
              repoId,
              version,
              episodeId,
            );
            videosInfo = extractVideoInfoV3WithSegmentation(
              repoId,
              version,
              info,
              episodeMetadata,
            );
          } else {
            // For v2.x, use simpler video info extraction
            if (info.video_path) {
              const chunkSize = Math.max(1, info.chunks_size || 1000);
              const episode_chunk = Math.floor(episodeId / chunkSize);
              videosInfo = Object.entries(info.features)
                .filter(([, value]) => value.dtype === "video")
                .map(([key, value]) => {
                  const videoPath = formatStringWithVars(info.video_path!, {
                    video_key: key,
                    episode_chunk: episode_chunk
                      .toString()
                      .padStart(PADDING.CHUNK_INDEX, "0"),
                    episode_index: episodeId
                      .toString()
                      .padStart(PADDING.EPISODE_INDEX, "0"),
                  });
                  return {
                    filename: key,
                    url: buildVersionedUrl(repoId, version, videoPath),
                    isGrayscale: isGrayscaleShape(value.shape),
                  };
                });
            }
          }

          adjacentVideos.push({ episodeId, videosInfo });
        } catch {
          // Skip failed episodes silently
        }
      }
    }

    return adjacentVideos;
  } catch {
    // Return empty array on error
    return [];
  }
}

// Legacy v2.x data loading
async function getEpisodeDataV2(
  repoId: string,
  version: string,
  info: DatasetMetadata,
  episodeId: number,
): Promise<EpisodeData> {
  if (!info.data_path) {
    throw new Error("v2 dataset metadata is missing data_path");
  }
  const chunkSize = Math.max(1, info.chunks_size || 1000);
  const episode_chunk = Math.floor(episodeId / chunkSize);

  const datasetInfo: DatasetDisplayInfo = {
    repoId,
    total_frames: info.total_frames,
    total_episodes: info.total_episodes,
    fps: info.fps,
    robot_type: null,
    codebase_version: version,
    total_tasks: 0,
    dataset_size_mb: 0,
    cameras: [],
  };

  // Generate list of episodes
  const episodes =
    process.env.EPISODES === undefined
      ? Array.from({ length: datasetInfo.total_episodes }, (_, i) => i)
      : [
          ...new Set(
            process.env.EPISODES.split(/\s+/)
              .map((value) => Number(value.trim()))
              .filter(
                (value) =>
                  Number.isInteger(value) &&
                  value >= 0 &&
                  value < datasetInfo.total_episodes,
              ),
          ),
        ].sort((a, b) => a - b);

  // Videos information
  const videosInfo =
    info.video_path !== null
      ? Object.entries(info.features)
          .filter(([, value]) => value.dtype === "video")
          .map(([key, value]) => {
            const videoPath = formatStringWithVars(info.video_path!, {
              video_key: key,
              episode_chunk: episode_chunk
                .toString()
                .padStart(PADDING.CHUNK_INDEX, "0"),
              episode_index: episodeId
                .toString()
                .padStart(PADDING.EPISODE_INDEX, "0"),
            });
            return {
              filename: key,
              url: buildVersionedUrl(repoId, version, videoPath),
              isGrayscale: isGrayscaleShape(value.shape),
            };
          })
      : [];

  // Column data
  const columnNames = Object.entries(info.features)
    .filter(
      ([, value]) =>
        isChartNumericDType(value.dtype) && value.shape.length === 1,
    )
    .map(([key, { shape }]) => ({ key, length: shape[0] }));

  // Exclude specific columns
  const excludedColumns = EXCLUDED_COLUMNS.V2 as readonly string[];
  const filteredColumns = columnNames.filter(
    (column) => !excludedColumns.includes(column.key),
  );
  const columns: ColumnDef[] = filteredColumns.map(({ key }) => {
    let column_names: unknown = info.features[key].names;
    while (typeof column_names === "object" && column_names !== null) {
      if (Array.isArray(column_names)) break;
      column_names = Object.values(column_names)[0];
    }
    return {
      key,
      value: Array.isArray(column_names)
        ? column_names.map(
            (name: string) => `${key}${SERIES_NAME_DELIMITER}${name}`,
          )
        : Array.from(
            { length: columnNames.find((c) => c.key === key)?.length ?? 1 },
            (_, i) => `${key}${CHART_CONFIG.SERIES_NAME_DELIMITER}${i}`,
          ),
    };
  });

  const parquetUrl = buildVersionedUrl(
    repoId,
    version,
    formatStringWithVars(info.data_path, {
      episode_chunk: episode_chunk
        .toString()
        .padStart(PADDING.CHUNK_INDEX, "0"),
      episode_index: episodeId.toString().padStart(PADDING.EPISODE_INDEX, "0"),
    }),
  );

  const arrayBuffer = await fetchParquetFile(parquetUrl);
  const parquetColumns = Array.from(
    new Set([
      "timestamp",
      "task",
      "task_index",
      "language_instruction",
      // v3.1 language schema (lerobot#3467) — list<struct{...}> columns the
      // annotations panel renders / edits. lerobot can write these onto v2.x
      // datasets too (the codebase_version stays 2.x), so a v2 episode may
      // carry annotations. Requesting columns absent from the parquet schema
      // is a no-op in hyparquet, so this is safe for un-annotated datasets.
      "language_persistent",
      "language_events",
      ...filteredColumns.map((c) => c.key),
    ]),
  );
  const allData = await readParquetAsObjects(arrayBuffer, parquetColumns);

  // Frame timestamps (sorted, seconds) let the annotations editor snap atoms
  // to exact frames. v3.1 language atoms are broadcast in `language_persistent`
  // and fired per-row in `language_events`; extract them so annotations written
  // onto v2.x datasets render in the panel/timeline just like on v3.0.
  const frameTimestamps = [
    ...new Set(
      allData
        .map((row) => toFiniteNumber(row.timestamp))
        .filter((value): value is number => value !== null),
    ),
  ].sort((a, b) => a - b);
  const languageAtoms = extractLanguageAtoms(allData);

  // Extract task from language_instruction fields, task field, or tasks.jsonl
  let task: string | undefined;

  if (allData.length > 0) {
    task = extractLanguageInstructions(allData, [
      0,
      Math.floor(allData.length / 2),
      allData.length - 1,
    ]);
  }

  if (!task && allData.length > 0) {
    const tasks = normalizeTaskList(allData[0].task ?? allData[0].tasks);
    if (tasks.length > 0) task = tasks.join("\n");
  }

  if (!task && allData.length > 0) {
    try {
      const definitions = await loadTaskDefinitions(repoId, version);
      const taskIndex = taskIndexNumber(allData[0].task_index);
      if (taskIndex !== null) task = definitions.byIndex.get(taskIndex);
    } catch {
      // Task metadata is optional for legacy datasets.
    }
  }

  // Build chart data from already-parsed allData (no second parquet parse)
  const seriesNames = [
    "timestamp",
    ...columns.map(({ value }) => value).flat(),
  ];

  const chartData = allData.map((row) => {
    const obj: Record<string, number> = {};
    obj["timestamp"] = toFiniteNumber(row.timestamp) ?? 0;
    for (const col of columns) {
      const rawVal = row[col.key];
      if (Array.isArray(rawVal)) {
        rawVal.forEach((v: unknown, i: number) => {
          const value = toFiniteNumber(v);
          if (i < col.value.length && value !== null) {
            obj[col.value[i]] = value;
          }
        });
      } else if (rawVal !== undefined) {
        const value = toFiniteNumber(rawVal);
        if (value !== null) obj[col.value[0]] = value;
      }
    }
    return obj;
  });
  const sampledChartData = evenlySampleArray(chartData, MAX_EPISODE_POINTS);

  // List of columns that are ignored (e.g., 2D or 3D data)
  const ignoredColumns = Object.entries(info.features)
    .filter(
      ([, value]) => isChartNumericDType(value.dtype) && value.shape.length > 1,
    )
    .map(([key]) => key);

  // Process chart data into organized groups using utility function
  const chartGroups = processChartDataGroups(seriesNames, sampledChartData);

  const duration =
    sampledChartData.length > 0
      ? sampledChartData[sampledChartData.length - 1].timestamp
      : 0;

  const chartDataGroups = chartGroups.map((group) =>
    sampledChartData.map((row) => {
      const grouped = groupRowBySuffix(pick(row, [...group, "timestamp"]));
      // Ensure timestamp is always a number at the top level
      return {
        ...grouped,
        timestamp:
          typeof grouped.timestamp === "number" ? grouped.timestamp : 0,
      };
    }),
  );

  return {
    datasetInfo,
    episodeId,
    videosInfo,
    chartDataGroups,
    flatChartData: sampledChartData,
    episodes,
    ignoredColumns,
    duration,
    task,
    languageAtoms,
    frameTimestamps,
  };
}

// v3.0 implementation with segmentation support for all episodes
async function getEpisodeDataV3(
  repoId: string,
  version: string,
  info: DatasetMetadata,
  episodeId: number,
): Promise<EpisodeData> {
  const datasetInfo: DatasetDisplayInfo = {
    repoId,
    total_frames: info.total_frames,
    total_episodes: info.total_episodes,
    fps: info.fps,
    robot_type: null,
    codebase_version: version,
    total_tasks: 0,
    dataset_size_mb: 0,
    cameras: [],
  };

  const episodes = Array.from({ length: info.total_episodes }, (_, i) => i);

  // Load episode metadata to get timestamps for episode 0
  const episodeMetadata = await loadEpisodeMetadataV3Simple(
    repoId,
    version,
    episodeId,
  );

  // Create video info with segmentation using the metadata
  const videosInfo = extractVideoInfoV3WithSegmentation(
    repoId,
    version,
    info,
    episodeMetadata,
  );

  // Load episode data for charts
  const {
    chartDataGroups,
    flatChartData,
    ignoredColumns,
    task,
    languageAtoms,
    frameTimestamps,
  } = await loadEpisodeDataV3(repoId, version, info, episodeMetadata);

  const duration = episodeMetadata.length
    ? episodeMetadata.length / info.fps
    : episodeMetadata.video_to_timestamp - episodeMetadata.video_from_timestamp;

  return {
    datasetInfo,
    episodeId,
    videosInfo,
    chartDataGroups,
    flatChartData,
    episodes,
    ignoredColumns,
    duration,
    task,
    languageAtoms,
    frameTimestamps,
  };
}

// Load episode data for v3.0 charts
async function loadEpisodeDataV3(
  repoId: string,
  version: string,
  info: DatasetMetadata,
  episodeMetadata: EpisodeMetadataV3,
): Promise<{
  chartDataGroups: ChartRow[][];
  flatChartData: Record<string, number>[];
  ignoredColumns: string[];
  task?: string;
  languageAtoms?: import("@/types/language.types").LanguageAtom[];
  frameTimestamps?: number[];
}> {
  // Build data file path using chunk and file indices
  const dataChunkIndex = bigIntToNumber(episodeMetadata.data_chunk_index, 0);
  const dataFileIndex = bigIntToNumber(episodeMetadata.data_file_index, 0);
  const dataPath = buildV3DataPath(dataChunkIndex, dataFileIndex);

  try {
    const dataUrl = buildVersionedUrl(repoId, version, dataPath);
    const parquetFile = await fetchParquetFile(dataUrl);
    const v3DataColumns = Array.from(
      new Set([
        "index",
        "timestamp",
        "task_index",
        "language_instruction",
        "language_instruction_2",
        "language_instruction_3",
        // v3.1 language schema (lerobot#3467) — list<struct{...}> columns
        // that the annotations panel renders / edits.
        "language_persistent",
        "language_events",
        ...Object.entries(info.features)
          .filter(([, feature]) => {
            const dtype = feature.dtype.toLowerCase();
            const isNumericOrBool = [
              "float32",
              "float16",
              "float64",
              "int8",
              "int16",
              "int32",
              "int64",
              "uint8",
              "uint16",
              "uint32",
              "uint64",
              "bool",
              "boolean",
            ].includes(dtype);
            return isNumericOrBool && feature.shape.length <= 1;
          })
          .map(([key]) => key),
      ]),
    );
    // Extract the episode-specific data slice
    const fromIndex = bigIntToNumber(episodeMetadata.dataset_from_index, 0);
    let toIndex = bigIntToNumber(episodeMetadata.dataset_to_index, fromIndex);
    if (toIndex <= fromIndex) {
      toIndex = fromIndex + 1;
    }

    let episodeRows: Record<string, unknown>[] = [];
    let usedRowRange = false;

    try {
      const indexPreview = await readParquetAsObjects(parquetFile, ["index"], {
        rowStart: 0,
        rowEnd: 1,
      });
      const startIndexValue = indexPreview[0]?.index;
      if (startIndexValue !== undefined && startIndexValue !== null) {
        const fileStartIndex = toFiniteNumber(startIndexValue) ?? 0;
        const localFromIndex = Math.max(0, fromIndex - fileStartIndex);
        const localToIndex = Math.max(localFromIndex, toIndex - fileStartIndex);
        episodeRows = await readParquetAsObjects(parquetFile, v3DataColumns, {
          rowStart: localFromIndex,
          rowEnd: localToIndex,
        });
        usedRowRange = true;
      }
    } catch {
      // Fall back to full reads if row-range selection fails.
    }

    if (!usedRowRange) {
      episodeRows = await readParquetAsObjects(parquetFile, v3DataColumns);
    }

    // Extract frame timestamps from the *full* (non-sampled) row set so the
    // annotations editor can snap to the exact frame the user is on.
    const frameTimestamps = [
      ...new Set(
        episodeRows
          .map((row) => toFiniteNumber(row.timestamp))
          .filter((value): value is number => value !== null),
      ),
    ].sort((a, b) => a - b);

    const episodeData = evenlySampleArray(episodeRows, MAX_EPISODE_POINTS);

    if (episodeData.length === 0) {
      return {
        chartDataGroups: [],
        flatChartData: [],
        ignoredColumns: [],
        task: undefined,
        languageAtoms: extractLanguageAtoms(episodeRows),
        frameTimestamps,
      };
    }

    // Convert to the same format as v2.x for compatibility with existing chart code
    const { chartDataGroups, flatChartData, ignoredColumns } =
      processEpisodeDataForCharts(episodeData, info, episodeMetadata);

    // Prefer the authoritative `tasks` list on the episode's own metadata
    // (v3.0 stores it as list[str] — see lerobot dataset_metadata.save_episode).
    let task: string | undefined;
    if (episodeMetadata.tasks && episodeMetadata.tasks.length > 0) {
      task = episodeMetadata.tasks.join("\n");
    }

    // Fall back to per-frame language_instruction fields
    if (!task && episodeData.length > 0) {
      task = extractLanguageInstructions(episodeRows, [
        0,
        Math.floor(episodeRows.length / 2),
        episodeRows.length - 1,
      ]);
    }

    // Fall back to the authoritative task_index in the frame data. The task
    // table may be Parquet or JSONL and its rows are not guaranteed to be
    // ordered by task_index.
    if (!task && episodeData.length > 0) {
      try {
        const definitions = await loadTaskDefinitions(repoId, version);
        const indexes = new Set<number>();
        for (const row of episodeData) {
          const index = taskIndexNumber(row.task_index);
          if (index !== null) indexes.add(index);
        }
        const names = [...indexes]
          .sort((a, b) => a - b)
          .map((index) => definitions.byIndex.get(index))
          .filter((name): name is string => !!name);
        if (names.length > 0) task = [...new Set(names)].join("\n");
      } catch {
        // Could not load optional task metadata.
      }
    }

    return {
      chartDataGroups,
      flatChartData,
      ignoredColumns,
      task,
      languageAtoms: extractLanguageAtoms(episodeRows),
      frameTimestamps,
    };
  } catch {
    return {
      chartDataGroups: [],
      flatChartData: [],
      ignoredColumns: [],
      task: undefined,
    };
  }
}

/**
 * Extract `LanguageAtom`s from a list of v3.1 parquet rows.
 *
 * Each row may carry two arrays:
 *   - `language_persistent`: broadcast — same content on every row in the
 *     episode. We sample row 0 (skipping nulls/empties).
 *   - `language_events`: per-row, mostly empty. We collect every event whose
 *     row's `timestamp` falls within the episode.
 *
 * Hyparquet decodes `list<struct<...>>` to `Array<Record<string, unknown>>`.
 * We coerce loosely-typed values to the canonical `LanguageAtom` shape and
 * drop rows that don't validate.
 */
export function extractLanguageAtoms(
  episodeRows: Record<string, unknown>[],
): import("@/types/language.types").LanguageAtom[] {
  if (!episodeRows.length) return [];
  type LanguageAtom = import("@/types/language.types").LanguageAtom;
  const atoms: LanguageAtom[] = [];
  const seenPersistent: Set<string> = new Set(); // dedupe broadcast copies

  const coerce = (raw: unknown, fallbackTs?: number): LanguageAtom | null => {
    if (!raw || typeof raw !== "object") return null;
    const r = raw as Record<string, unknown>;
    const role =
      typeof r.role === "string" ? (r.role as LanguageAtom["role"]) : null;
    if (!role) return null;
    const style =
      typeof r.style === "string" ? (r.style as LanguageAtom["style"]) : null;
    const content = typeof r.content === "string" ? r.content : null;
    const timestamp = toFiniteNumber(r.timestamp) ?? fallbackTs ?? 0;
    const tool_calls = Array.isArray(r.tool_calls)
      ? (r.tool_calls as LanguageAtom["tool_calls"])
      : null;
    const camera =
      typeof r.camera === "string" && r.camera.length > 0 ? r.camera : null;
    return { role, content, style, timestamp, camera, tool_calls };
  };

  // Persistent slice: read once from the first row that has a non-empty list.
  for (const row of episodeRows) {
    const list = row["language_persistent"];
    if (Array.isArray(list) && list.length > 0) {
      for (const raw of list) {
        const atom = coerce(raw);
        if (!atom) continue;
        const key = `${atom.style}|${atom.role}|${atom.timestamp}|${atom.camera ?? ""}|${atom.content}`;
        if (seenPersistent.has(key)) continue;
        seenPersistent.add(key);
        atoms.push(atom);
      }
      break;
    }
  }

  // Event slice: every non-empty per-row list contributes its rows. Each
  // event's timestamp should already match its frame timestamp; we use the
  // row timestamp as a fallback if it's missing.
  for (const row of episodeRows) {
    const list = row["language_events"];
    if (!Array.isArray(list) || list.length === 0) continue;
    const rowTs = toFiniteNumber(row.timestamp) ?? 0;
    for (const raw of list) {
      const atom = coerce(raw, rowTs);
      if (atom) atoms.push(atom);
    }
  }

  return atoms;
}

// Process episode data for charts (v3.0 compatible)
function processEpisodeDataForCharts(
  episodeData: Record<string, unknown>[],
  info: DatasetMetadata,
  episodeMetadata?: EpisodeMetadataV3,
): {
  chartDataGroups: ChartRow[][];
  flatChartData: Record<string, number>[];
  ignoredColumns: string[];
} {
  // Convert parquet data to chart format
  let seriesNames: string[] = [];

  // Dynamically create a mapping from numeric indices to feature names based on actual dataset features
  const v3IndexToFeatureMap: Record<string, string> = {};

  // Build mapping based on what features actually exist in the dataset
  const featureKeys = Object.keys(info.features);

  // Common feature order for v3.0 datasets (but only include if they exist)
  const expectedFeatureOrder = [
    "observation.state",
    "action",
    "timestamp",
    "episode_index",
    "frame_index",
    "next.reward",
    "next.done",
    "index",
    "task_index",
  ];

  // Map indices to features that actually exist
  let currentIndex = 0;
  expectedFeatureOrder.forEach((feature) => {
    if (featureKeys.includes(feature)) {
      v3IndexToFeatureMap[currentIndex.toString()] = feature;
      currentIndex++;
    }
  });

  // Columns to exclude from charts (note: 'task' is intentionally not excluded as we want to access it)
  const excludedColumns = EXCLUDED_COLUMNS.V3 as readonly string[];

  // Create columns structure similar to V2.1 for proper hierarchical naming
  const columns: ColumnDef[] = Object.entries(info.features)
    .filter(
      ([key, value]) =>
        isChartNumericDType(value.dtype) &&
        value.shape.length === 1 &&
        !excludedColumns.includes(key),
    )
    .map(([key, feature]) => {
      let column_names: unknown = feature.names;
      while (typeof column_names === "object" && column_names !== null) {
        if (Array.isArray(column_names)) break;
        column_names = Object.values(column_names)[0];
      }
      return {
        key,
        value: Array.isArray(column_names)
          ? column_names.map(
              (name: string) => `${key}${SERIES_NAME_DELIMITER}${name}`,
            )
          : Array.from(
              { length: feature.shape[0] || 1 },
              (_, i) => `${key}${CHART_CONFIG.SERIES_NAME_DELIMITER}${i}`,
            ),
      };
    });

  // First, extract all series from the first data row to understand the structure
  if (episodeData.length > 0) {
    const firstRow = episodeData[0];
    const allKeys: string[] = [];

    Object.entries(firstRow || {}).forEach(([key, value]) => {
      if (key === "timestamp") return; // Skip timestamp, we'll add it separately

      // Map numeric key to feature name if available
      const featureName = v3IndexToFeatureMap[key] || key;

      // Skip if feature doesn't exist in dataset
      if (!info.features[featureName]) return;

      // Skip excluded columns
      if (excludedColumns.includes(featureName)) return;

      // Find the matching column definition to get proper names
      const columnDef = columns.find((col) => col.key === featureName);
      if (columnDef && Array.isArray(value) && value.length > 0) {
        // Use the proper hierarchical naming from column definition
        columnDef.value.forEach((seriesName, idx) => {
          if (idx < value.length) {
            allKeys.push(seriesName);
          }
        });
      } else {
        const numericValue = toFiniteNumber(value);
        if (numericValue !== null || typeof value === "boolean") {
          // For scalar numeric values
          allKeys.push(featureName);
        }
      }
    });

    seriesNames = ["timestamp", ...allKeys];
  } else {
    // Fallback to column-based approach like V2.1
    seriesNames = ["timestamp", ...columns.map(({ value }) => value).flat()];
  }

  const chartData = episodeData.map((row, index) => {
    const obj: Record<string, number> = {};

    // Add timestamp aligned with video timing
    // For v3.0, we need to map the episode data index to the actual video duration
    let videoDuration = episodeData.length; // Fallback to data length
    if (episodeMetadata) {
      // Use actual video segment duration if available
      videoDuration =
        (episodeMetadata.video_to_timestamp || 30) -
        (episodeMetadata.video_from_timestamp || 0);
    }
    obj["timestamp"] =
      (index / Math.max(episodeData.length - 1, 1)) * videoDuration;

    // Add all data columns using hierarchical naming
    if (row && typeof row === "object") {
      Object.entries(row).forEach(([key, value]) => {
        if (key === "timestamp") {
          // Timestamp is already handled above
          return;
        }

        // Map numeric key to feature name if available
        const featureName = v3IndexToFeatureMap[key] || key;

        // Skip if feature doesn't exist in dataset
        if (!info.features[featureName]) return;

        // Skip excluded columns
        if (excludedColumns.includes(featureName)) return;

        // Find the matching column definition to get proper series names
        const columnDef = columns.find((col) => col.key === featureName);

        if (Array.isArray(value) && columnDef) {
          // For array values like observation.state and action, use proper hierarchical naming
          value.forEach((val, idx) => {
            if (idx < columnDef.value.length) {
              const seriesName = columnDef.value[idx];
              const numericValue = toFiniteNumber(val);
              if (numericValue !== null) {
                obj[seriesName] = numericValue;
              }
            }
          });
        } else {
          const numericValue = toFiniteNumber(value);
          if (numericValue !== null) {
            obj[featureName] = numericValue;
          } else if (typeof value === "boolean") {
            // Convert boolean to number for charts
            obj[featureName] = value ? 1 : 0;
          }
        }
      });
    }

    return obj;
  });

  // List of columns that are ignored (now we handle 2D data by flattening)
  const ignoredColumns = [
    ...Object.entries(info.features)
      .filter(
        ([, value]) =>
          isChartNumericDType(value.dtype) && value.shape.length > 2, // Only ignore 3D+ data
      )
      .map(([key]) => key),
    ...excludedColumns, // Also include the manually excluded columns
  ];

  // Process chart data into organized groups using utility function
  const chartGroups = processChartDataGroups(seriesNames, chartData);

  const chartDataGroups = chartGroups.map((group) =>
    chartData.map((row) => {
      const grouped = groupRowBySuffix(pick(row, [...group, "timestamp"]));
      // Ensure timestamp is always a number at the top level
      return {
        ...grouped,
        timestamp:
          typeof grouped.timestamp === "number" ? grouped.timestamp : 0,
      };
    }),
  );

  return { chartDataGroups, flatChartData: chartData, ignoredColumns };
}

// Video info extraction with segmentation for v3.0
function extractVideoInfoV3WithSegmentation(
  repoId: string,
  version: string,
  info: DatasetMetadata,
  episodeMetadata: EpisodeMetadataV3,
): VideoInfo[] {
  // Get video features from dataset info
  const videoFeatures = Object.entries(info.features).filter(
    ([, value]) => value.dtype === "video",
  );

  const videosInfo = videoFeatures.map(([videoKey, videoFeature]) => {
    // Check if we have per-camera metadata in the episode row
    const cameraSpecificKeys = Object.keys(episodeMetadata).filter((key) =>
      key.startsWith(`videos/${videoKey}/`),
    );

    let chunkIndex: number,
      fileIndex: number,
      segmentStart: number,
      segmentEnd: number;

    const toNum = (v: string | number | string[] | undefined): number => {
      return toFiniteNumber(v) ?? 0;
    };

    if (cameraSpecificKeys.length > 0) {
      chunkIndex = toNum(episodeMetadata[`videos/${videoKey}/chunk_index`]);
      fileIndex = toNum(episodeMetadata[`videos/${videoKey}/file_index`]);
      segmentStart =
        toNum(episodeMetadata[`videos/${videoKey}/from_timestamp`]) || 0;
      segmentEnd =
        toNum(episodeMetadata[`videos/${videoKey}/to_timestamp`]) || 30;
    } else {
      chunkIndex = episodeMetadata.video_chunk_index || 0;
      fileIndex = episodeMetadata.video_file_index || 0;
      segmentStart = episodeMetadata.video_from_timestamp || 0;
      segmentEnd = episodeMetadata.video_to_timestamp || 30;
    }

    // Convert BigInt to number for timestamps
    const startNum = bigIntToNumber(segmentStart);
    const endNum = bigIntToNumber(segmentEnd);

    const videoPath = buildV3VideoPath(
      videoKey,
      bigIntToNumber(chunkIndex, 0),
      bigIntToNumber(fileIndex, 0),
    );
    const fullUrl = buildVersionedUrl(repoId, version, videoPath);

    return {
      filename: videoKey,
      url: fullUrl,
      // Enable segmentation with timestamps from metadata
      isSegmented: true,
      segmentStart: startNum,
      segmentEnd: endNum,
      segmentDuration: endNum - startNum,
      isGrayscale: isGrayscaleShape(videoFeature.shape),
    };
  });

  return videosInfo;
}

// Walks v3.0 episode-metadata parquet files across chunks/files. A new chunk
// begins when the current chunk's files run out (404 or empty); iteration ends
// when file-000 of the next chunk 404s. `chunks_size` caps files per chunk, so
// large datasets can spill past chunk-000.
async function* iterateEpisodeMetadataFilesV3(
  repoId: string,
  version: string,
): AsyncGenerator<Record<string, unknown>[], void, unknown> {
  let chunkIndex = 0;
  let fileIndex = 0;

  while (true) {
    const path = buildV3EpisodesMetadataPath(chunkIndex, fileIndex);
    const url = buildVersionedUrl(repoId, version, path);
    let rows: Record<string, unknown>[];
    try {
      const buf = await fetchParquetFile(url);
      rows = await readParquetAsObjects(buf, []);
    } catch {
      if (fileIndex === 0) return;
      chunkIndex++;
      fileIndex = 0;
      continue;
    }

    if (rows.length === 0) {
      if (fileIndex === 0) return;
      chunkIndex++;
      fileIndex = 0;
      continue;
    }

    yield rows;
    fileIndex++;
  }
}

// Metadata loading for v3.0 episodes
async function loadEpisodeMetadataV3Simple(
  repoId: string,
  version: string,
  episodeId: number,
): Promise<EpisodeMetadataV3> {
  for await (const rows of iterateEpisodeMetadataFilesV3(repoId, version)) {
    for (const row of rows) {
      const parsed = parseEpisodeRowSimple(row);
      if (parsed.episode_index === episodeId) {
        return parsed;
      }
    }
  }
  throw new Error(`Episode ${episodeId} not found in metadata`);
}

// Simple parser for episode row - focuses on key fields for episodes.
// Parquet readers may return numbers, BigInts or numeric strings depending on
// the physical schema, so every index/timestamp goes through one converter.
function parseEpisodeRowSimple(
  row: Record<string, unknown>,
): EpisodeMetadataV3 {
  const numberOr = (value: unknown, fallback = 0): number => {
    const converted = toFiniteNumber(value);
    return converted === null ? fallback : converted;
  };
  const integerOr = (value: unknown, fallback = 0): number =>
    Math.trunc(numberOr(value, fallback));

  if (!row || typeof row !== "object") {
    return {
      episode_index: 0,
      data_chunk_index: 0,
      data_file_index: 0,
      dataset_from_index: 0,
      dataset_to_index: 0,
      video_chunk_index: 0,
      video_file_index: 0,
      video_from_timestamp: 0,
      video_to_timestamp: 30,
      length: 30,
    };
  }

  const named = "episode_index" in row;
  const episodeIndex = named
    ? integerOr(row.episode_index)
    : integerOr(row["0"]);
  const dataChunkIndex = named
    ? integerOr(row["data/chunk_index"] ?? row.data_chunk_index)
    : integerOr(row["1"]);
  const dataFileIndex = named
    ? integerOr(row["data/file_index"] ?? row.data_file_index)
    : integerOr(row["2"]);
  const fromIndex = named
    ? integerOr(row.dataset_from_index)
    : integerOr(row["3"]);
  const toIndex = named ? integerOr(row.dataset_to_index) : integerOr(row["4"]);
  const length = named
    ? integerOr(row.length, Math.max(0, toIndex - fromIndex))
    : integerOr(row["9"], 30);

  const videoKeys = Object.keys(row).filter(
    (key) => key.includes("videos/") && key.includes("/chunk_index"),
  );
  let videoChunkIndex = named
    ? integerOr(row.video_chunk_index)
    : integerOr(row["5"]);
  let videoFileIndex = named
    ? integerOr(row.video_file_index)
    : integerOr(row["6"]);
  let videoFromTs = named
    ? numberOr(row.video_from_timestamp)
    : numberOr(row["7"]);
  let videoToTs = named
    ? numberOr(row.video_to_timestamp, 30)
    : numberOr(row["8"], 30);
  if (videoKeys.length > 0) {
    const base = videoKeys[0].replace("/chunk_index", "");
    videoChunkIndex = integerOr(row[`${base}/chunk_index`], videoChunkIndex);
    videoFileIndex = integerOr(row[`${base}/file_index`], videoFileIndex);
    videoFromTs = numberOr(row[`${base}/from_timestamp`], videoFromTs);
    videoToTs = numberOr(row[`${base}/to_timestamp`], videoToTs);
  }

  const tasks = [
    ...normalizeTaskList(row.tasks),
    ...normalizeTaskList(row.task),
  ].filter((task, index, values) => values.indexOf(task) === index);
  const taskIndex = toFiniteNumber(row.task_index);
  const episodeData: EpisodeMetadataV3 = {
    episode_index: episodeIndex,
    data_chunk_index: dataChunkIndex,
    data_file_index: dataFileIndex,
    dataset_from_index: fromIndex,
    dataset_to_index: toIndex,
    length,
    video_chunk_index: videoChunkIndex,
    video_file_index: videoFileIndex,
    video_from_timestamp: videoFromTs,
    video_to_timestamp: videoToTs,
    ...(tasks.length > 0 ? { tasks } : {}),
    ...(taskIndex !== null && Number.isInteger(taskIndex)
      ? { task_index: taskIndex }
      : {}),
  };

  // Preserve per-camera metadata needed for segmented video playback.
  for (const key of Object.keys(row)) {
    if (!key.startsWith("videos/")) continue;
    const value = row[key];
    episodeData[key] =
      typeof value === "bigint"
        ? Number(value)
        : typeof value === "number" || typeof value === "string"
          ? value
          : 0;
  }
  return episodeData;
}

// ─── Stats computation ───────────────────────────────────────────

/**
 * Compute per-column min/max values from the current episode's chart data.
 */
export function computeColumnMinMax(
  chartDataGroups: ChartRow[][],
): ColumnMinMax[] {
  const stats: Record<string, { min: number; max: number }> = {};

  for (const group of chartDataGroups) {
    for (const row of group) {
      for (const [key, value] of Object.entries(row)) {
        if (key === "timestamp") continue;
        if (typeof value === "number" && isFinite(value)) {
          if (!stats[key]) {
            stats[key] = { min: value, max: value };
          } else {
            if (value < stats[key].min) stats[key].min = value;
            if (value > stats[key].max) stats[key].max = value;
          }
        } else if (typeof value === "object" && value !== null) {
          // Nested group like { joint_0: 1.2, joint_1: 3.4 }
          for (const [subKey, subVal] of Object.entries(value)) {
            const fullKey = `${key} | ${subKey}`;
            if (typeof subVal === "number" && isFinite(subVal)) {
              if (!stats[fullKey]) {
                stats[fullKey] = { min: subVal, max: subVal };
              } else {
                if (subVal < stats[fullKey].min) stats[fullKey].min = subVal;
                if (subVal > stats[fullKey].max) stats[fullKey].max = subVal;
              }
            }
          }
        }
      }
    }
  }

  return Object.entries(stats).map(([column, { min, max }]) => ({
    column,
    min: Math.round(min * 1000) / 1000,
    max: Math.round(max * 1000) / 1000,
  }));
}

function normalizeTaskList(raw: unknown): string[] {
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed) return [];
    // Some Parquet writers serialize list[str] as a JSON string.
    if (trimmed.startsWith("[") && trimmed.endsWith("]")) {
      try {
        return normalizeTaskList(JSON.parse(trimmed));
      } catch {
        // Treat malformed JSON as the literal task text below.
      }
    }
    return [trimmed];
  }
  if (!Array.isArray(raw)) return [];
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    for (const task of normalizeTaskList(item)) {
      if (seen.has(task)) continue;
      seen.add(task);
      result.push(task);
    }
  }
  return result;
}

function taskIndexNumber(value: unknown): number | null {
  const number = toFiniteNumber(value);
  return number !== null && Number.isInteger(number) && number >= 0
    ? number
    : null;
}

function normalizeTaskIndices(raw: unknown): number[] {
  const values = Array.isArray(raw) ? raw : [raw];
  const result: number[] = [];
  const seen = new Set<number>();
  for (const value of values) {
    const index = taskIndexNumber(value);
    if (index === null || seen.has(index)) continue;
    seen.add(index);
    result.push(index);
  }
  return result;
}

function taskTextFromRow(row: Record<string, unknown>): string | null {
  for (const key of [
    "task",
    "tasks",
    "task_name",
    "__index_level_0__",
    "name",
    "instruction",
    "language_instruction",
  ]) {
    const task = normalizeTaskList(row[key])[0];
    if (task) return task;
  }
  return null;
}

type TaskDefinition = { taskIndex: number | null; task: string; order: number };
type TaskDefinitions = {
  ordered: string[];
  byIndex: Map<number, string>;
};

/** Build a stable task list without assuming Parquet row order is task_index. */
export function buildTaskDefinitions(
  rows: Record<string, unknown>[],
): TaskDefinitions {
  const definitions: TaskDefinition[] = [];
  rows.forEach((row, order) => {
    const task = taskTextFromRow(row);
    if (!task) return;
    definitions.push({
      taskIndex: taskIndexNumber(row.task_index),
      task,
      order,
    });
  });

  const hasExplicitIndices = definitions.some(
    (definition) => definition.taskIndex !== null,
  );
  if (!hasExplicitIndices) {
    // Old tasks tables used the dataframe index as the task index. Preserve
    // that compatibility only when no authoritative task_index column exists.
    definitions.forEach((definition, index) => {
      definition.taskIndex = index;
    });
  }

  const byIndex = new Map<number, string>();
  for (const definition of [...definitions].sort(
    (a, b) =>
      (a.taskIndex ?? Number.MAX_SAFE_INTEGER) -
        (b.taskIndex ?? Number.MAX_SAFE_INTEGER) || a.order - b.order,
  )) {
    if (definition.taskIndex !== null && !byIndex.has(definition.taskIndex)) {
      byIndex.set(definition.taskIndex, definition.task);
    }
  }

  const ordered: string[] = [];
  const seen = new Set<string>();
  for (const definition of [...definitions].sort(
    (a, b) =>
      (a.taskIndex ?? Number.MAX_SAFE_INTEGER) -
        (b.taskIndex ?? Number.MAX_SAFE_INTEGER) || a.order - b.order,
  )) {
    if (seen.has(definition.task)) continue;
    seen.add(definition.task);
    ordered.push(definition.task);
  }
  return { ordered, byIndex };
}

function parseJsonlRows(text: string): Record<string, unknown>[] {
  const rows: Record<string, unknown>[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const value: unknown = JSON.parse(trimmed);
      if (value && typeof value === "object" && !Array.isArray(value)) {
        rows.push(value as Record<string, unknown>);
      }
    } catch {
      // A single corrupt line should not hide all valid task definitions.
    }
  }
  return rows;
}

async function loadTaskDefinitions(
  repoId: string,
  version: string,
): Promise<TaskDefinitions> {
  const normalizedVersion = normalizeDatasetVersion(version);
  if (!normalizedVersion) return { ordered: [], byIndex: new Map() };
  const v3 = isDatasetV3(normalizedVersion);
  const paths = v3
    ? ["meta/tasks.parquet", "meta/tasks.jsonl"]
    : ["meta/tasks.jsonl", "meta/tasks.parquet"];
  for (const path of paths) {
    try {
      let rows: Record<string, unknown>[];
      if (path.endsWith(".parquet")) {
        const buffer = await fetchParquetFile(
          buildVersionedUrl(repoId, normalizedVersion, path),
        );
        rows = await readParquetAsObjects(buffer, []);
      } else {
        const response = await fetch(
          buildVersionedUrl(repoId, normalizedVersion, path),
          {
            cache: "no-store",
            headers: authHeaders(
              buildVersionedUrl(repoId, normalizedVersion, path),
            ),
          },
        );
        if (!response.ok) continue;
        rows = parseJsonlRows(await response.text());
      }
      const definitions = buildTaskDefinitions(rows);
      if (definitions.ordered.length > 0) return definitions;
    } catch {
      // Try the alternate metadata representation.
    }
  }
  return { ordered: [], byIndex: new Map() };
}

function addEpisodeTasks(
  episodeTasks: Record<number, string[]>,
  episodeIndex: number,
  tasks: string[],
): void {
  if (
    !Number.isInteger(episodeIndex) ||
    episodeIndex < 0 ||
    tasks.length === 0
  ) {
    return;
  }
  const current = episodeTasks[episodeIndex] ?? [];
  const seen = new Set(current);
  for (const task of tasks) {
    if (seen.has(task)) continue;
    seen.add(task);
    current.push(task);
  }
  episodeTasks[episodeIndex] = current;
}

function tasksFromDataRows(
  rows: Record<string, unknown>[],
  definitions: TaskDefinitions,
  episodeIndex?: number,
): string[] {
  const names: string[] = [];
  const seen = new Set<string>();
  const add = (task: string | null) => {
    if (!task || seen.has(task)) return;
    seen.add(task);
    names.push(task);
  };
  for (const row of rows) {
    if (
      episodeIndex !== undefined &&
      taskIndexNumber(row.episode_index) !== null &&
      taskIndexNumber(row.episode_index) !== episodeIndex
    ) {
      continue;
    }
    for (const task of normalizeTaskList(row.task ?? row.tasks)) add(task);
    const index = taskIndexNumber(row.task_index);
    if (index !== null) add(definitions.byIndex.get(index) ?? null);
  }
  return names;
}

async function readTaskRowsFromParquet(
  url: string,
): Promise<Record<string, unknown>[]> {
  const buffer = await fetchParquetFile(url);
  // hyparquet rejects projections containing a missing column. Try the
  // smallest useful projections first so datasets without an optional
  // episode_index/task column still expose their authoritative task_index.
  const projections = [
    ["index", "episode_index", "task_index", "task"],
    ["index", "task_index"],
    ["episode_index", "task_index"],
    ["task_index"],
    ["index", "episode_index", "task"],
    ["index", "task"],
    ["episode_index", "task"],
    ["task"],
  ];
  for (const columns of projections) {
    try {
      return await readParquetAsObjects(buffer, columns);
    } catch {
      // Try a projection with fewer optional columns.
    }
  }
  return [];
}

async function loadV3FrameTaskMappings(
  repoId: string,
  version: string,
  refs: Array<{ episode: EpisodeMetadataV3; hasTasks: boolean }>,
  definitions: TaskDefinitions,
  episodeTasks: Record<number, string[]>,
): Promise<void> {
  const unresolved = refs.filter(
    ({ episode, hasTasks }) =>
      !hasTasks &&
      (episode.dataset_to_index > episode.dataset_from_index ||
        definitions.ordered.length > 0),
  );
  if (unresolved.length === 0) return;

  const byFile = new Map<
    string,
    Array<{ episode: EpisodeMetadataV3; hasTasks: boolean }>
  >();
  for (const ref of unresolved) {
    const key = `${ref.episode.data_chunk_index}-${ref.episode.data_file_index}`;
    const bucket = byFile.get(key) ?? [];
    bucket.push(ref);
    byFile.set(key, bucket);
  }

  const fileResults = await mapWithConcurrency(
    [...byFile.values()],
    Math.min(CROSS_EPISODE_FETCH_CONCURRENCY, 8),
    async (fileRefs) => {
      const first = fileRefs[0].episode;
      const path = buildV3DataPath(
        first.data_chunk_index,
        first.data_file_index,
      );
      try {
        const rows = await readTaskRowsFromParquet(
          buildVersionedUrl(repoId, version, path),
        );
        return fileRefs.map((ref) => {
          const episode = ref.episode;
          const from = episode.dataset_from_index;
          const to = episode.dataset_to_index;
          const indexedRows = rows.filter((row) => {
            const rowEpisode = taskIndexNumber(row.episode_index);
            if (rowEpisode !== null)
              return rowEpisode === episode.episode_index;
            const rowIndex = toFiniteNumber(row.index);
            return rowIndex !== null && rowIndex >= from && rowIndex < to;
          });
          const selected =
            indexedRows.length > 0
              ? indexedRows
              : rows.slice(
                  Math.max(0, from - (toFiniteNumber(rows[0]?.index) ?? from)),
                  Math.max(0, to - (toFiniteNumber(rows[0]?.index) ?? from)),
                );
          return {
            episodeIndex: episode.episode_index,
            tasks: tasksFromDataRows(
              selected,
              definitions,
              episode.episode_index,
            ),
          };
        });
      } catch {
        return [];
      }
    },
  );
  for (const mappings of fileResults) {
    for (const mapping of mappings) {
      addEpisodeTasks(episodeTasks, mapping.episodeIndex, mapping.tasks);
    }
  }
}

async function loadV2FrameTaskMappings(
  repoId: string,
  version: string,
  info: DatasetMetadata | undefined,
  episodeIds: number[],
  definitions: TaskDefinitions,
  episodeTasks: Record<number, string[]>,
): Promise<void> {
  if (!info?.data_path || episodeIds.length === 0) return;
  const chunkSize = Math.max(1, toFiniteNumber(info.chunks_size) ?? 1000);
  const paths = episodeIds.map((episodeIndex) => {
    const episodeChunk = Math.floor(episodeIndex / chunkSize);
    return {
      episodeIndex,
      path: formatStringWithVars(info.data_path!, {
        episode_chunk: episodeChunk.toString().padStart(3, "0"),
        episode_index: episodeIndex.toString().padStart(6, "0"),
      }),
    };
  });
  const results = await mapWithConcurrency(
    paths,
    Math.min(CROSS_EPISODE_FETCH_CONCURRENCY, 8),
    async ({ episodeIndex, path }) => {
      try {
        const rows = await readTaskRowsFromParquet(
          buildVersionedUrl(repoId, version, path),
        );
        return {
          episodeIndex,
          tasks: tasksFromDataRows(rows, definitions, episodeIndex),
        };
      } catch {
        return { episodeIndex, tasks: [] };
      }
    },
  );
  for (const result of results) {
    addEpisodeTasks(episodeTasks, result.episodeIndex, result.tasks);
  }
}

const taskIndexCache = new Map<
  string,
  { data: DatasetTaskIndex | null; expiry: number }
>();
const TASK_INDEX_TTL_MS = 5 * 60 * 1000;
const MAX_TASK_INDEX_CACHE_ENTRIES = 64;
function pruneTaskIndexCache(now: number): void {
  for (const [key, value] of taskIndexCache) {
    if (now >= value.expiry) taskIndexCache.delete(key);
  }
  while (taskIndexCache.size > MAX_TASK_INDEX_CACHE_ENTRIES) {
    const oldestKey = taskIndexCache.keys().next().value;
    if (!oldestKey) break;
    taskIndexCache.delete(oldestKey);
  }
}
function clearTaskIndexCache(): void {
  taskIndexCache.clear();
}
if (typeof window !== "undefined") {
  window.addEventListener("levi:hf-auth-changed", clearTaskIndexCache);
}

/**
 * Build the dataset-wide task ↔ episode mapping.
 *
 * The mapping accepts all LeRobot layouts: v2 JSONL metadata, v3 episode
 * metadata with a `tasks` list, and v3 datasets (for example Libero) where the
 * only authoritative association is each frame's `task_index`. Task tables are
 * matched by their task_index column; physical row order is used only when the
 * column is absent. The raw dataset remains read-only.
 */
export async function loadDatasetTaskIndex(
  repoId: string,
  version: string,
  info?: DatasetMetadata,
): Promise<DatasetTaskIndex | null> {
  const normalizedVersion = normalizeDatasetVersion(version);
  if (!normalizedVersion) return null;
  const now = Date.now();
  pruneTaskIndexCache(now);
  const cacheKey = `${repoId}@${normalizedVersion}@${info?.total_episodes ?? "unknown"}@${info?.data_path ?? ""}`;
  const cached = taskIndexCache.get(cacheKey);
  if (cached && now < cached.expiry) {
    taskIndexCache.delete(cacheKey);
    taskIndexCache.set(cacheKey, cached);
    return cached.data;
  }

  let data: DatasetTaskIndex | null = null;
  try {
    const definitions = await loadTaskDefinitions(repoId, normalizedVersion);
    const episodeTasks: Record<number, string[]> = {};
    const ordered = [...definitions.ordered];
    const seen = new Set(ordered);
    const addTask = (task: string) => {
      if (!seen.has(task)) {
        seen.add(task);
        ordered.push(task);
      }
    };

    if (isDatasetV3(normalizedVersion)) {
      const refs: Array<{ episode: EpisodeMetadataV3; hasTasks: boolean }> = [];
      for await (const rows of iterateEpisodeMetadataFilesV3(
        repoId,
        normalizedVersion,
      )) {
        for (const row of rows) {
          const episode = parseEpisodeRowSimple(row);
          if (
            !Number.isInteger(episode.episode_index) ||
            episode.episode_index < 0
          ) {
            continue;
          }
          const explicit = normalizeTaskList(episode.tasks);
          const indexed = normalizeTaskIndices(episode.task_index);
          const fromIndices = indexed
            .map((index) => definitions.byIndex.get(index))
            .filter((task): task is string => !!task);
          const tasks = [...new Set([...explicit, ...fromIndices])];
          addEpisodeTasks(episodeTasks, episode.episode_index, tasks);
          tasks.forEach(addTask);
          refs.push({ episode, hasTasks: tasks.length > 0 });
        }
      }
      await loadV3FrameTaskMappings(
        repoId,
        normalizedVersion,
        refs,
        definitions,
        episodeTasks,
      );
    } else {
      const episodesResponse = await fetch(
        buildVersionedUrl(repoId, normalizedVersion, "meta/episodes.jsonl"),
        {
          headers: authHeaders(
            buildVersionedUrl(repoId, normalizedVersion, "meta/episodes.jsonl"),
          ),
          cache: "no-store",
        },
      );
      const episodeRows = episodesResponse.ok
        ? parseJsonlRows(await episodesResponse.text())
        : [];
      const episodeIds: number[] = [];
      for (const row of episodeRows) {
        const episodeIndex = taskIndexNumber(row.episode_index);
        if (episodeIndex === null) continue;
        episodeIds.push(episodeIndex);
        const explicit = [
          ...normalizeTaskList(row.tasks),
          ...normalizeTaskList(row.task),
        ].filter((task, index, values) => values.indexOf(task) === index);
        const indexed = normalizeTaskIndices(
          row.task_index ?? row.task_indices ?? row.tasks_index,
        );
        const fromIndices = indexed
          .map((index) => definitions.byIndex.get(index))
          .filter((task): task is string => !!task);
        const tasks = [...new Set([...explicit, ...fromIndices])];
        addEpisodeTasks(episodeTasks, episodeIndex, tasks);
        tasks.forEach(addTask);
      }
      const ids =
        episodeIds.length > 0
          ? [...new Set(episodeIds)]
          : Array.from(
              { length: Math.max(0, info?.total_episodes ?? 0) },
              (_, i) => i,
            );
      const unresolved = ids.filter(
        (episodeIndex) => !episodeTasks[episodeIndex]?.length,
      );
      await loadV2FrameTaskMappings(
        repoId,
        normalizedVersion,
        info,
        unresolved,
        definitions,
        episodeTasks,
      );
    }

    for (const tasks of Object.values(episodeTasks)) tasks.forEach(addTask);
    data = ordered.length > 0 ? { tasks: ordered, episodeTasks } : null;
  } catch {
    data = null;
  }

  taskIndexCache.set(cacheKey, {
    data,
    expiry: Date.now() + TASK_INDEX_TTL_MS,
  });
  pruneTaskIndexCache(Date.now());
  return data;
}

export type EpisodeOutcome = "success" | "failure";

const episodeOutcomesCache = new Map<
  string,
  { data: Record<string, EpisodeOutcome>; expiry: number }
>();
function pruneEpisodeOutcomesCache(now: number): void {
  for (const [key, value] of episodeOutcomesCache) {
    if (now >= value.expiry) episodeOutcomesCache.delete(key);
  }
  while (episodeOutcomesCache.size > MAX_TASK_INDEX_CACHE_ENTRIES) {
    const oldestKey = episodeOutcomesCache.keys().next().value;
    if (!oldestKey) break;
    episodeOutcomesCache.delete(oldestKey);
  }
}
function clearEpisodeOutcomesCache(): void {
  episodeOutcomesCache.clear();
}
if (typeof window !== "undefined") {
  window.addEventListener("levi:hf-auth-changed", clearEpisodeOutcomesCache);
}

/**
 * Per-episode success/failure label for datasets converted from a
 * policy-eval rollout capture (LEVI's built-in converter writes it as
 * `levi_outcome` on each `meta/episodes.jsonl` row — see
 * `levi/conversion/raw.py:demo_outcome`). Dataset-native metadata, not a
 * LEVI annotation-layer sidecar, so this reads the dataset's own files
 * directly and needs no annotation backend. v3 datasets (LEVI's converter
 * only ever emits v2.1) and datasets without the field both simply return
 * an empty map.
 */
export async function loadEpisodeOutcomes(
  repoId: string,
  version: string,
): Promise<Record<string, EpisodeOutcome>> {
  const normalizedVersion = normalizeDatasetVersion(version);
  if (!normalizedVersion || isDatasetV3(normalizedVersion)) return {};
  const now = Date.now();
  pruneEpisodeOutcomesCache(now);
  const cacheKey = `${repoId}@${normalizedVersion}`;
  const cached = episodeOutcomesCache.get(cacheKey);
  if (cached && now < cached.expiry) {
    episodeOutcomesCache.delete(cacheKey);
    episodeOutcomesCache.set(cacheKey, cached);
    return cached.data;
  }

  let data: Record<string, EpisodeOutcome> = {};
  try {
    const response = await fetch(
      buildVersionedUrl(repoId, normalizedVersion, "meta/episodes.jsonl"),
      {
        headers: authHeaders(
          buildVersionedUrl(repoId, normalizedVersion, "meta/episodes.jsonl"),
        ),
        cache: "no-store",
      },
    );
    if (response.ok) {
      for (const row of parseJsonlRows(await response.text())) {
        const episodeIndex = taskIndexNumber(row.episode_index);
        const outcome = row.levi_outcome;
        if (
          episodeIndex !== null &&
          (outcome === "success" || outcome === "failure")
        ) {
          data[String(episodeIndex)] = outcome;
        }
      }
    }
  } catch {
    data = {};
  }

  episodeOutcomesCache.set(cacheKey, {
    data,
    expiry: Date.now() + TASK_INDEX_TTL_MS,
  });
  pruneEpisodeOutcomesCache(Date.now());
  return data;
}

/**
 * Load episode lengths from v2 JSONL or all v3 metadata parquet chunks.
 * Returns min/max/mean/median/std and a histogram, or null if unavailable.
 */
export async function loadAllEpisodeLengthsV3(
  repoId: string,
  version: string,
  fps: number,
): Promise<EpisodeLengthStats | null> {
  const normalizedVersion = normalizeDatasetVersion(version);
  if (!normalizedVersion) return null;
  try {
    const allEpisodes: { index: number; length: number }[] = [];
    const seenEpisodes = new Set<number>();
    if (!Number.isFinite(fps) || fps <= 0) return null;
    const append = (indexValue: unknown, lengthValue: unknown) => {
      const index = taskIndexNumber(indexValue);
      const lengthNumber = toFiniteNumber(lengthValue);
      if (
        index === null ||
        lengthNumber === null ||
        !Number.isInteger(lengthNumber) ||
        lengthNumber < 0 ||
        seenEpisodes.has(index)
      ) {
        return;
      }
      seenEpisodes.add(index);
      allEpisodes.push({ index, length: lengthNumber });
    };
    if (!isDatasetV3(normalizedVersion)) {
      const response = await fetch(
        buildVersionedUrl(repoId, normalizedVersion, "meta/episodes.jsonl"),
        {
          headers: authHeaders(
            buildVersionedUrl(repoId, normalizedVersion, "meta/episodes.jsonl"),
          ),
        },
      );
      if (!response.ok) return null;
      for (const row of parseJsonlRows(await response.text())) {
        append(row.episode_index, row.length);
      }
    } else {
      for await (const rows of iterateEpisodeMetadataFilesV3(
        repoId,
        normalizedVersion,
      )) {
        for (const row of rows) {
          const parsed = parseEpisodeRowSimple(row);
          append(parsed.episode_index, parsed.length);
        }
      }
    }

    if (allEpisodes.length === 0) return null;

    const withSeconds = allEpisodes.map((ep) => ({
      episodeIndex: ep.index,
      frames: ep.length,
      lengthSeconds: Math.round((ep.length / fps) * 100) / 100,
    }));

    const sortedByLength = [...withSeconds].sort(
      (a, b) => a.lengthSeconds - b.lengthSeconds,
    );
    const shortestEpisodes = sortedByLength.slice(0, 5);
    const longestEpisodes = sortedByLength.slice(-5).reverse();

    const lengths = withSeconds.map((e) => e.lengthSeconds);
    const sum = lengths.reduce((a, b) => a + b, 0);
    const exactMean = sum / lengths.length;
    const mean = Math.round(exactMean * 100) / 100;

    const sorted = [...lengths].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    const median =
      sorted.length % 2 === 0
        ? Math.round(((sorted[mid - 1] + sorted[mid]) / 2) * 100) / 100
        : sorted[mid];

    const variance =
      lengths.reduce((acc, l) => acc + (l - exactMean) ** 2, 0) /
      lengths.length;
    const std = Math.round(Math.sqrt(variance) * 100) / 100;

    // Build histogram
    const histMin = Math.min(...lengths);
    const histMax = Math.max(...lengths);

    if (histMax === histMin) {
      return {
        shortestEpisodes,
        longestEpisodes,
        allEpisodeLengths: withSeconds,
        meanEpisodeLength: mean,
        medianEpisodeLength: median,
        stdEpisodeLength: std,
        episodeLengthHistogram: [
          { binLabel: `${histMin.toFixed(1)}s`, count: lengths.length },
        ],
      };
    }

    const p1 = sorted[Math.floor(sorted.length * 0.01)];
    const p99 = sorted[Math.ceil(sorted.length * 0.99) - 1];
    const range = p99 - p1 || 1;

    const targetBins = Math.max(
      10,
      Math.min(50, Math.ceil(Math.log2(lengths.length) + 1)),
    );
    const rawBinWidth = range / targetBins;
    const magnitude = Math.pow(10, Math.floor(Math.log10(rawBinWidth)));
    const niceSteps = [1, 2, 2.5, 5, 10];
    const niceBinWidth =
      niceSteps.map((s) => s * magnitude).find((w) => w >= rawBinWidth) ??
      rawBinWidth;

    const niceMin = Math.floor(p1 / niceBinWidth) * niceBinWidth;
    const niceMax = Math.ceil(p99 / niceBinWidth) * niceBinWidth;
    const actualBinCount = Math.max(
      1,
      Math.round((niceMax - niceMin) / niceBinWidth),
    );
    const bins = Array.from({ length: actualBinCount }, () => 0);

    for (const len of lengths) {
      let binIdx = Math.floor((len - niceMin) / niceBinWidth);
      if (binIdx < 0) binIdx = 0;
      if (binIdx >= actualBinCount) binIdx = actualBinCount - 1;
      bins[binIdx]++;
    }

    const histogram = bins.map((count, i) => {
      const lo = niceMin + i * niceBinWidth;
      const hi = lo + niceBinWidth;
      return { binLabel: `${lo.toFixed(1)}–${hi.toFixed(1)}s`, count };
    });

    return {
      shortestEpisodes,
      longestEpisodes,
      allEpisodeLengths: withSeconds,
      meanEpisodeLength: mean,
      medianEpisodeLength: median,
      stdEpisodeLength: std,
      episodeLengthHistogram: histogram,
    };
  } catch {
    return null;
  }
}

/**
 * Load video frame info for all episodes across all cameras.
 * Returns camera names + a map of camera → EpisodeFrameInfo[].
 */
export async function loadAllEpisodeFrameInfo(
  repoId: string,
  version: string,
  info: DatasetMetadata,
): Promise<EpisodeFramesData> {
  const normalizedVersion = normalizeDatasetVersion(version);
  if (!normalizedVersion) return { cameras: [], framesByCamera: {} };
  const videoFeatures = Object.entries(info.features).filter(
    ([, f]) => f.dtype === "video",
  );
  if (videoFeatures.length === 0) return { cameras: [], framesByCamera: {} };

  const cameras = videoFeatures.map(([key]) => key);
  const framesByCamera: Record<string, EpisodeFrameInfo[]> = {};
  for (const cam of cameras) framesByCamera[cam] = [];
  const sampledEpisodeSet = buildSampledEpisodeSet(
    info.total_episodes,
    MAX_FRAMES_OVERVIEW_EPISODES,
  );

  if (isDatasetV3(normalizedVersion)) {
    for await (const rows of iterateEpisodeMetadataFilesV3(
      repoId,
      normalizedVersion,
    )) {
      for (const row of rows) {
        const epIdx = taskIndexNumber(row["episode_index"]);
        if (epIdx === null || epIdx >= info.total_episodes) continue;
        if (sampledEpisodeSet && !sampledEpisodeSet.has(epIdx)) continue;
        for (const cam of cameras) {
          if (framesByCamera[cam].some((frame) => frame.episodeIndex === epIdx))
            continue;
          const cIdx =
            taskIndexNumber(
              row[`videos/${cam}/chunk_index`] ?? row["video_chunk_index"],
            ) ?? 0;
          const fIdx =
            taskIndexNumber(
              row[`videos/${cam}/file_index`] ?? row["video_file_index"],
            ) ?? 0;
          const fromTs =
            toFiniteNumber(
              row[`videos/${cam}/from_timestamp`] ??
                row["video_from_timestamp"],
            ) ?? 0;
          const toTs =
            toFiniteNumber(
              row[`videos/${cam}/to_timestamp`] ?? row["video_to_timestamp"],
            ) ?? 30;
          if (toTs < fromTs) continue;
          const videoPath = `videos/${cam}/chunk-${cIdx.toString().padStart(3, "0")}/file-${fIdx.toString().padStart(3, "0")}.mp4`;
          framesByCamera[cam].push({
            episodeIndex: epIdx,
            videoUrl: buildVersionedUrl(repoId, normalizedVersion, videoPath),
            firstFrameTime: fromTs,
            lastFrameTime: Math.max(0, toTs - 0.05),
          });
        }
      }
    }
    return { cameras, framesByCamera };
  }

  // v2.x — construct URLs from template
  if (!info.video_path) return { cameras, framesByCamera };
  for (let i = 0; i < info.total_episodes; i++) {
    if (sampledEpisodeSet && !sampledEpisodeSet.has(i)) continue;
    const chunk = Math.floor(i / (info.chunks_size || 1000));
    for (const cam of cameras) {
      const videoPath = formatStringWithVars(info.video_path, {
        video_key: cam,
        episode_chunk: chunk.toString().padStart(3, "0"),
        episode_index: i.toString().padStart(6, "0"),
      });
      framesByCamera[cam].push({
        episodeIndex: i,
        videoUrl: buildVersionedUrl(repoId, version, videoPath),
        firstFrameTime: 0,
        lastFrameTime: null,
      });
    }
  }
  return { cameras, framesByCamera };
}

// ─── Cross-episode action variance ──────────────────────────────

export type LowMovementEpisode = {
  episodeIndex: number;
  totalMovement: number;
};

export type AggVelocityStat = {
  name: string;
  std: number; // normalized by motor range
  maxAbs: number; // normalized by motor range
  bins: number[];
  lo: number; // normalized by motor range
  hi: number; // normalized by motor range
  motorRange: number;
  inactive?: boolean; // true if p95(|Δa|) < 1% of motor range
  discrete?: boolean; // true if motor has very few unique values (e.g. open/close gripper)
};

export type AggAutocorrelation = {
  chartData: Record<string, number>[];
  suggestedChunk: number | null;
  shortKeys: string[];
  /** Episodes long enough to contribute to the averaged ACF. */
  episodesUsed: number;
};

export type SpeedDistEntry = {
  episodeIndex: number;
  speed: number;
};

export type AggAlignment = {
  ccData: { lag: number; max: number; mean: number; min: number }[];
  meanPeakLag: number;
  meanPeakCorr: number;
  maxPeakLag: number;
  maxPeakCorr: number;
  minPeakLag: number;
  minPeakCorr: number;
  lagRangeMin: number;
  lagRangeMax: number;
  numPairs: number;
};

export type JerkyEpisode = {
  episodeIndex: number;
  meanAbsDelta: number;
};

/**
 * Which episodes an Action Insights run covers.
 * - `all`: every episode in the dataset
 * - `range`: an inclusive episode-index window
 * - `task`: every episode whose metadata lists this task string
 */
export type CrossEpisodeScope =
  | { kind: "all" }
  | { kind: "range"; from: number; to: number }
  | { kind: "task"; task: string };

export type CrossEpisodeRequest = {
  scope: CrossEpisodeScope;
  /** null → analyse every episode in scope (still bounded by the ceiling). */
  maxEpisodes: number | null;
};

export type CrossEpisodeLoadOptions = Partial<CrossEpisodeRequest> & {
  numTimeBins?: number;
  /** Reports parquet-read progress; only meaningful for client-side calls. */
  onProgress?: (loaded: number, total: number) => void;
};

export type CrossEpisodeVarianceData = {
  actionNames: string[];
  timeBins: number[];
  variance: number[][];
  numEpisodes: number;
  lowMovementEpisodes: LowMovementEpisode[];
  aggVelocity: AggVelocityStat[];
  aggAutocorrelation: AggAutocorrelation | null;
  speedDistribution: SpeedDistEntry[];
  jerkyEpisodes: JerkyEpisode[];
  aggAlignment: AggAlignment | null;
  /** The scope this run covered, echoed back for display. */
  scope: CrossEpisodeScope;
  /** Episodes matching the scope, before any sampling. */
  scopeEpisodes: number;
  /** Episodes selected after applying the sample cap. */
  requestedEpisodes: number;
  /** True when the cap forced an even subsample of the scope. */
  sampled: boolean;
};

export async function loadCrossEpisodeActionVariance(
  repoId: string,
  version: string,
  info: DatasetMetadata,
  fps: number,
  options: CrossEpisodeLoadOptions = {},
): Promise<CrossEpisodeVarianceData | null> {
  const {
    scope = { kind: "all" },
    maxEpisodes = DEFAULT_CROSS_EPISODE_SAMPLE,
    numTimeBins = 50,
    onProgress,
  } = options;
  // `null` means "every episode in scope"; the ceiling is only a runaway guard.
  const normalizedVersion = normalizeDatasetVersion(version);
  if (!normalizedVersion) return null;
  if (!Number.isFinite(fps) || fps <= 0) return null;
  if (!isDatasetV3(normalizedVersion) && !info.data_path) return null;
  const requestedEpisodeBudget =
    maxEpisodes === null
      ? CROSS_EPISODE_SAMPLE_CEILING
      : typeof maxEpisodes === "number" && Number.isFinite(maxEpisodes)
        ? Math.trunc(maxEpisodes)
        : DEFAULT_CROSS_EPISODE_SAMPLE;
  const episodeBudget = Math.min(
    Math.max(2, requestedEpisodeBudget),
    CROSS_EPISODE_SAMPLE_CEILING,
  );
  const timeBinCount =
    typeof numTimeBins === "number" && Number.isFinite(numTimeBins)
      ? Math.max(2, Math.trunc(numTimeBins))
      : 50;
  const actionEntry = Object.entries(info.features).find(
    ([key, f]) => key === "action" && f.shape.length === 1,
  );
  if (!actionEntry) {
    console.warn(
      "[cross-ep] No action feature found. Available features:",
      Object.entries(info.features)
        .map(([k, f]) => `${k}(${f.dtype}, shape=${JSON.stringify(f.shape)})`)
        .join(", "),
    );
    return null;
  }

  const [actionKey, actionMeta] = actionEntry;
  const actionDim = actionMeta.shape[0];

  let names: unknown = actionMeta.names;
  while (typeof names === "object" && names !== null && !Array.isArray(names)) {
    names = Object.values(names)[0];
  }
  const actionNames = Array.isArray(names)
    ? (names as string[]).map((n) => `${actionKey}${SERIES_NAME_DELIMITER}${n}`)
    : Array.from(
        { length: actionDim },
        (_, i) => `${actionKey}${SERIES_NAME_DELIMITER}${i}`,
      );

  // State feature for alignment computation
  const stateEntry = Object.entries(info.features).find(
    ([key, f]) => key === "observation.state" && f.shape.length === 1,
  );
  const stateKey = stateEntry?.[0] ?? null;
  const stateDim = stateEntry?.[1].shape[0] ?? 0;

  // Collect episode metadata
  type EpMeta = {
    index: number;
    chunkIdx: number;
    fileIdx: number;
    from: number;
    to: number;
  };
  const allEps: EpMeta[] = [];
  const seenEpisodeIndices = new Set<number>();

  if (isDatasetV3(normalizedVersion)) {
    for await (const rows of iterateEpisodeMetadataFilesV3(
      repoId,
      normalizedVersion,
    )) {
      for (const row of rows) {
        const parsed = parseEpisodeRowSimple(row);
        if (
          parsed.episode_index < 0 ||
          parsed.episode_index >= info.total_episodes ||
          parsed.dataset_to_index <= parsed.dataset_from_index ||
          seenEpisodeIndices.has(parsed.episode_index)
        ) {
          continue;
        }
        seenEpisodeIndices.add(parsed.episode_index);
        allEps.push({
          index: parsed.episode_index,
          chunkIdx: parsed.data_chunk_index,
          fileIdx: parsed.data_file_index,
          from: parsed.dataset_from_index,
          to: parsed.dataset_to_index,
        });
      }
    }
  } else {
    for (let i = 0; i < info.total_episodes; i++) {
      allEps.push({ index: i, chunkIdx: 0, fileIdx: 0, from: 0, to: 0 });
    }
  }

  allEps.sort((a, b) => a.index - b.index);
  if (allEps.length < 2) {
    console.warn(
      `[cross-ep] Only ${allEps.length} episode(s) found in metadata, need ≥2`,
    );
    return null;
  }

  // Narrow to the requested scope before sampling, so a range or a task gets
  // the full episode budget instead of whatever survives a dataset-wide sample.
  const taskIndex =
    scope.kind === "task"
      ? await loadDatasetTaskIndex(repoId, normalizedVersion, info)
      : null;
  const rangeLo =
    scope.kind === "range" ? Math.min(scope.from, scope.to) : -Infinity;
  const rangeHi =
    scope.kind === "range" ? Math.max(scope.from, scope.to) : Infinity;
  const inScope = allEps.filter((ep) => {
    if (scope.kind === "range") {
      return ep.index >= rangeLo && ep.index <= rangeHi;
    }
    if (scope.kind === "task") {
      return (taskIndex?.episodeTasks[ep.index] ?? []).includes(scope.task);
    }
    return true;
  });

  if (inScope.length < 2) {
    console.warn(
      `[cross-ep] Scope ${JSON.stringify(scope)} matched ${inScope.length} episode(s) out of ${allEps.length}, need ≥2`,
    );
    return null;
  }
  console.log(
    `[cross-ep] Scope ${JSON.stringify(scope)} matched ${inScope.length}/${allEps.length} episodes, analysing up to ${episodeBudget}`,
  );

  // Sample episodes evenly across the scope when it exceeds the budget.
  const sampled = evenlySampleArray(inScope, episodeBudget);
  let loadedCount = 0;
  const reportProgress = (delta: number) => {
    loadedCount += delta;
    onProgress?.(loadedCount, sampled.length);
  };

  // Load action (and state) data per episode
  const episodeActions: { index: number; actions: number[][] }[] = [];
  const episodeStates: (number[][] | null)[] = [];

  if (isDatasetV3(normalizedVersion)) {
    const byFile = new Map<string, EpMeta[]>();
    for (const ep of sampled) {
      const key = `${ep.chunkIdx}-${ep.fileIdx}`;
      if (!byFile.has(key)) byFile.set(key, []);
      byFile.get(key)!.push(ep);
    }

    const fileResults = await mapWithConcurrency(
      [...byFile.values()],
      CROSS_EPISODE_FETCH_CONCURRENCY,
      async (eps) => {
        const ep0 = eps[0];
        const dataPath = `data/chunk-${ep0.chunkIdx.toString().padStart(3, "0")}/file-${ep0.fileIdx.toString().padStart(3, "0")}.parquet`;
        const fileEpActions: { index: number; actions: number[][] }[] = [];
        const fileEpStates: (number[][] | null)[] = [];
        try {
          const buf = await fetchParquetFile(
            buildVersionedUrl(repoId, normalizedVersion, dataPath),
          );
          const rows = await readParquetAsObjects(
            buf,
            stateKey ? ["index", actionKey, stateKey] : ["index", actionKey],
          );
          const fileStart =
            rows.length > 0 && rows[0].index !== undefined
              ? (toFiniteNumber(rows[0].index) ?? 0)
              : 0;

          for (const ep of eps) {
            const localFrom = Math.max(0, ep.from - fileStart);
            const localTo = Math.min(rows.length, ep.to - fileStart);
            const actions: number[][] = [];
            const states: number[][] = [];
            for (let r = localFrom; r < localTo; r++) {
              const raw = rows[r]?.[actionKey];
              if (Array.isArray(raw))
                actions.push(raw.map((value) => toFiniteNumber(value) ?? 0));
              if (stateKey) {
                const sRaw = rows[r]?.[stateKey];
                if (Array.isArray(sRaw))
                  states.push(sRaw.map((value) => toFiniteNumber(value) ?? 0));
              }
            }
            if (actions.length > 0) {
              const sampledIndices = evenlySampleIndices(
                actions.length,
                Math.min(actions.length, MAX_CROSS_EPISODE_FRAMES_PER_EPISODE),
              );
              const sampledActions = sampledIndices.map((i) => actions[i]);
              const sampledStates =
                stateKey && states.length === actions.length
                  ? sampledIndices.map((i) => states[i])
                  : null;
              fileEpActions.push({ index: ep.index, actions: sampledActions });
              fileEpStates.push(stateKey ? sampledStates : null);
            }
          }
        } catch {
          /* skip file */
        }
        reportProgress(eps.length);
        return { fileEpActions, fileEpStates };
      },
    );
    for (const { fileEpActions, fileEpStates } of fileResults) {
      episodeActions.push(...fileEpActions);
      episodeStates.push(...fileEpStates);
    }
  } else {
    const chunkSize = info.chunks_size || 1000;
    const epResults = await mapWithConcurrency(
      sampled,
      CROSS_EPISODE_FETCH_CONCURRENCY,
      async (ep) => {
        const chunk = Math.floor(ep.index / chunkSize);
        const dataPath = formatStringWithVars(info.data_path, {
          episode_chunk: chunk.toString().padStart(3, "0"),
          episode_index: ep.index.toString().padStart(6, "0"),
        });
        try {
          const buf = await fetchParquetFile(
            buildVersionedUrl(repoId, normalizedVersion, dataPath),
          );
          const rows = await readParquetAsObjects(
            buf,
            stateKey ? [actionKey, stateKey] : [actionKey],
          );
          const actions: number[][] = [];
          const states: number[][] = [];
          for (const row of rows) {
            const raw = row[actionKey];
            if (Array.isArray(raw)) {
              actions.push(raw.map((value) => toFiniteNumber(value) ?? 0));
            } else {
              const vec: number[] = [];
              for (let d = 0; d < actionDim; d++) {
                const v = row[`${actionKey}.${d}`] ?? row[d];
                vec.push(toFiniteNumber(v) ?? 0);
              }
              actions.push(vec);
            }
            if (stateKey) {
              const sRaw = row[stateKey];
              if (Array.isArray(sRaw))
                states.push(sRaw.map((value) => toFiniteNumber(value) ?? 0));
            }
          }
          if (actions.length > 0) {
            const sampledIndices = evenlySampleIndices(
              actions.length,
              Math.min(actions.length, MAX_CROSS_EPISODE_FRAMES_PER_EPISODE),
            );
            const sampledActions = sampledIndices.map((i) => actions[i]);
            const sampledStates =
              stateKey && states.length === actions.length
                ? sampledIndices.map((i) => states[i])
                : null;
            return {
              index: ep.index,
              actions: sampledActions,
              states: sampledStates,
            };
          }
        } catch {
          /* skip */
        } finally {
          reportProgress(1);
        }
        return null;
      },
    );
    for (const result of epResults) {
      if (result !== null) {
        episodeActions.push({ index: result.index, actions: result.actions });
        episodeStates.push(stateKey ? result.states : null);
      }
    }
  }

  if (episodeActions.length < 2) {
    console.warn(
      `[cross-ep] Only ${episodeActions.length} episode(s) had loadable action data out of ${sampled.length} sampled`,
    );
    return null;
  }
  console.log(
    `[cross-ep] Loaded action data for ${episodeActions.length}/${sampled.length} episodes`,
  );

  // Resample each episode to numTimeBins and compute variance
  const timeBins = Array.from(
    { length: timeBinCount },
    (_, i) => i / (timeBinCount - 1),
  );
  const sums = Array.from(
    { length: timeBinCount },
    () => new Float64Array(actionDim),
  );
  const sumsSq = Array.from(
    { length: timeBinCount },
    () => new Float64Array(actionDim),
  );
  const counts = new Uint32Array(timeBinCount);

  for (const { actions: epActions } of episodeActions) {
    const T = epActions.length;
    for (let b = 0; b < timeBinCount; b++) {
      const srcIdx = Math.min(Math.round(timeBins[b] * (T - 1)), T - 1);
      const row = epActions[srcIdx];
      for (let d = 0; d < actionDim; d++) {
        const v = row[d] ?? 0;
        sums[b][d] += v;
        sumsSq[b][d] += v * v;
      }
      counts[b]++;
    }
  }

  const variance: number[][] = [];
  for (let b = 0; b < timeBinCount; b++) {
    const row: number[] = [];
    const n = counts[b];
    for (let d = 0; d < actionDim; d++) {
      if (n < 2) {
        row.push(0);
        continue;
      }
      const mean = sums[b][d] / n;
      row.push(sumsSq[b][d] / n - mean * mean);
    }
    variance.push(row);
  }

  // Per-episode average movement per frame: mean L2 norm of frame-to-frame action deltas
  const movementScores: LowMovementEpisode[] = episodeActions.map(
    ({ index, actions: ep }) => {
      if (ep.length < 2) return { episodeIndex: index, totalMovement: 0 };
      let total = 0;
      for (let t = 1; t < ep.length; t++) {
        let sumSq = 0;
        for (let d = 0; d < actionDim; d++) {
          const delta = (ep[t][d] ?? 0) - (ep[t - 1][d] ?? 0);
          sumSq += delta * delta;
        }
        total += Math.sqrt(sumSq);
      }
      const avgPerFrame = total / (ep.length - 1);
      return {
        episodeIndex: index,
        totalMovement: Math.round(avgPerFrame * 10000) / 10000,
      };
    },
  );

  movementScores.sort((a, b) => a.totalMovement - b.totalMovement);
  const lowMovementEpisodes = movementScores.slice(0, 10);

  // Precompute per-dimension normalization: motor range (max − min) and unique value count
  const motorRanges: number[] = new Array(actionDim);
  const motorUniqueCount: number[] = new Array(actionDim);
  const DISCRETE_THRESHOLD = 4; // ≤ this many unique values → discrete motor
  for (let d = 0; d < actionDim; d++) {
    let lo = Infinity,
      hi = -Infinity;
    const uniqueVals = new Set<number>();
    for (const { actions: ep } of episodeActions) {
      for (let t = 0; t < ep.length; t++) {
        const v = ep[t][d] ?? 0;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
        if (uniqueVals.size <= DISCRETE_THRESHOLD) uniqueVals.add(v);
      }
    }
    motorRanges[d] = hi - lo || 1;
    motorUniqueCount[d] = uniqueVals.size;
  }

  // Per-episode, per-dimension activity: p95(|Δa|) >= 1% of motor range
  const ACTIVITY_THRESHOLD = 0.001; // 0.1% of motor range
  // activeMap[episodeIdx][dimIdx] = true if motor d is active in that episode
  const activeMap: boolean[][] = episodeActions.map(({ actions: ep }) => {
    const flags: boolean[] = new Array(actionDim);
    for (let d = 0; d < actionDim; d++) {
      if (ep.length < 2) {
        flags[d] = false;
        continue;
      }
      const absDeltas: number[] = [];
      for (let t = 1; t < ep.length; t++) {
        absDeltas.push(Math.abs((ep[t][d] ?? 0) - (ep[t - 1][d] ?? 0)));
      }
      absDeltas.sort((a, b) => a - b);
      const p95 = absDeltas[Math.floor(absDeltas.length * 0.95)];
      flags[d] = p95 >= motorRanges[d] * ACTIVITY_THRESHOLD;
    }
    return flags;
  });
  // A motor is globally inactive only if inactive in all episodes
  const globallyActive: boolean[] = new Array(actionDim);
  for (let d = 0; d < actionDim; d++) {
    globallyActive[d] = activeMap.some((flags) => flags[d]);
  }

  // Aggregated velocity stats: pool deltas from all episodes, normalized by motor range
  const shortName = (k: string) => {
    const p = k.split(SERIES_NAME_DELIMITER);
    return p.length > 1 ? p[p.length - 1] : k;
  };

  const aggVelocity: AggVelocityStat[] = (() => {
    const binCount = 30;
    const results: AggVelocityStat[] = [];
    for (let d = 0; d < actionDim; d++) {
      const motorRange = motorRanges[d];
      const inactive = !globallyActive[d];
      // Collect all deltas (unfiltered) for histogram display
      const allDeltas: number[] = [];
      // Collect only deltas from active episodes for stats
      const activeDeltas: number[] = [];
      for (let ei = 0; ei < episodeActions.length; ei++) {
        const ep = episodeActions[ei].actions;
        for (let t = 1; t < ep.length; t++) {
          const delta = (ep[t][d] ?? 0) - (ep[t - 1][d] ?? 0);
          allDeltas.push(delta);
          if (activeMap[ei][d]) activeDeltas.push(delta);
        }
      }
      const deltas = activeDeltas.length > 0 ? activeDeltas : allDeltas;
      const nUnique = motorUniqueCount[d];
      const discrete = nUnique <= DISCRETE_THRESHOLD;
      if (deltas.length === 0) {
        results.push({
          name: shortName(actionNames[d]),
          std: 0,
          maxAbs: 0,
          bins: new Array(binCount).fill(0),
          lo: 0,
          hi: 0,
          motorRange,
          inactive,
          discrete,
        });
        continue;
      }
      let sum = 0,
        maxAbsRaw = 0,
        loRaw = Infinity,
        hiRaw = -Infinity;
      for (const v of deltas) {
        sum += v;
        const a = Math.abs(v);
        if (a > maxAbsRaw) maxAbsRaw = a;
        if (v < loRaw) loRaw = v;
        if (v > hiRaw) hiRaw = v;
      }
      const mean = sum / deltas.length;
      let varSum = 0;
      for (const v of deltas) varSum += (v - mean) ** 2;
      const rawStd = Math.sqrt(varSum / deltas.length);
      const std = rawStd / motorRange;
      const maxAbs = maxAbsRaw / motorRange;
      const lo = loRaw / motorRange;
      const hi = hiRaw / motorRange;
      const range = hi - lo || 1;
      const binW = range / binCount;
      const bins = new Array(binCount).fill(0);
      for (const v of deltas) {
        const normV = v / motorRange;
        let b = Math.floor((normV - lo) / binW);
        if (b >= binCount) b = binCount - 1;
        bins[b]++;
      }
      results.push({
        name: shortName(actionNames[d]),
        std,
        maxAbs,
        bins,
        lo,
        hi,
        motorRange,
        inactive,
        discrete,
      });
    }
    return results;
  })();

  // Aggregated autocorrelation: average per-episode ACFs
  const aggAutocorrelation: AggAutocorrelation | null = (() => {
    // Lag horizon comes from the 25th-percentile episode length, not the
    // shortest one: over a full dataset a single truncated episode would
    // otherwise drive maxLag below 2 and blank the chart entirely. Episodes
    // shorter than 2·maxLag are skipped below, so ~75% still contribute.
    const lengths = episodeActions
      .map((e) => e.actions.length)
      .sort((a, b) => a - b);
    const p25 = lengths[Math.floor(lengths.length * 0.25)] ?? 0;
    const maxLag = Math.min(100, Math.floor(p25 / 2));
    if (maxLag < 2) return null;

    const avgAcf: number[][] = Array.from({ length: actionDim }, () =>
      new Array(maxLag).fill(0),
    );
    let epCount = 0;

    for (const { actions: ep } of episodeActions) {
      if (ep.length < maxLag * 2) continue;
      epCount++;
      for (let d = 0; d < actionDim; d++) {
        const vals = ep.map((row) => row[d] ?? 0);
        const n = vals.length;
        const m = vals.reduce((a, b) => a + b, 0) / n;
        const centered = vals.map((v) => v - m);
        const vari = centered.reduce((a, v) => a + v * v, 0);
        if (vari === 0) continue;
        for (let lag = 1; lag <= maxLag; lag++) {
          let s = 0;
          for (let t = 0; t < n - lag; t++)
            s += centered[t] * centered[t + lag];
          avgAcf[d][lag - 1] += s / vari;
        }
      }
    }

    if (epCount === 0) return null;
    for (let d = 0; d < actionDim; d++)
      for (let l = 0; l < maxLag; l++) avgAcf[d][l] /= epCount;

    const shortKeys = actionNames.map(shortName);
    const chartData = Array.from({ length: maxLag }, (_, lag) => {
      const row: Record<string, number> = {
        lag: lag + 1,
        time: (lag + 1) / fps,
      };
      shortKeys.forEach((k, d) => {
        row[k] = avgAcf[d][lag];
      });
      return row;
    });

    // Suggested chunk: median lag where ACF drops below 0.5
    const lags = avgAcf
      .map((acf) => {
        const i = acf.findIndex((v) => v < 0.5);
        return i >= 0 ? i + 1 : null;
      })
      .filter(Boolean) as number[];
    const suggestedChunk =
      lags.length > 0
        ? lags.sort((a, b) => a - b)[Math.floor(lags.length / 2)]
        : null;

    return { chartData, suggestedChunk, shortKeys, episodesUsed: epCount };
  })();

  // Per-episode jerkiness: mean |Δa| across dimensions active in that episode, normalized by motor range
  const jerkyEpisodes: JerkyEpisode[] = episodeActions
    .map(({ index, actions: ep }, ei) => {
      let sum = 0,
        count = 0;
      for (let t = 1; t < ep.length; t++) {
        for (let d = 0; d < actionDim; d++) {
          if (!activeMap[ei][d]) continue; // skip motors inactive in this episode
          sum +=
            Math.abs((ep[t][d] ?? 0) - (ep[t - 1][d] ?? 0)) / motorRanges[d];
          count++;
        }
      }
      return { episodeIndex: index, meanAbsDelta: count > 0 ? sum / count : 0 };
    })
    .sort((a, b) => b.meanAbsDelta - a.meanAbsDelta);

  // Speed distribution: all episode movement scores (not just lowest 10)
  const speedDistribution: SpeedDistEntry[] = movementScores.map((s) => ({
    episodeIndex: s.episodeIndex,
    speed: s.totalMovement,
  }));

  // Aggregated state-action alignment across episodes
  const aggAlignment: AggAlignment | null = (() => {
    if (!stateKey || stateDim === 0) return null;

    let sNms: unknown = stateEntry![1].names;
    while (typeof sNms === "object" && sNms !== null && !Array.isArray(sNms))
      sNms = Object.values(sNms)[0];
    const stateNames = Array.isArray(sNms)
      ? (sNms as string[])
      : Array.from({ length: stateDim }, (_, i) => `${i}`);
    const actionSuffixes = actionNames.map((n) => {
      const p = n.split(SERIES_NAME_DELIMITER);
      return p[p.length - 1];
    });

    // Match pairs by suffix, fall back to index
    const pairs: [number, number][] = [];
    for (let ai = 0; ai < actionDim; ai++) {
      const si = stateNames.findIndex((s) => s === actionSuffixes[ai]);
      if (si >= 0) pairs.push([ai, si]);
    }
    if (pairs.length === 0) {
      const count = Math.min(actionDim, stateDim);
      for (let i = 0; i < count; i++) pairs.push([i, i]);
    }
    if (pairs.length === 0) return null;

    const maxLag = 30;
    const numLags = 2 * maxLag + 1;
    const corrSums = pairs.map(() => new Float64Array(numLags));
    const corrCounts = pairs.map(() => new Uint32Array(numLags));

    for (let ei = 0; ei < episodeActions.length; ei++) {
      const states = episodeStates[ei];
      if (!states) continue;
      const { actions } = episodeActions[ei];
      const n = Math.min(actions.length, states.length);
      if (n < 10) continue;

      for (let pi = 0; pi < pairs.length; pi++) {
        const [ai, si] = pairs[pi];
        const aDeltas = Array.from(
          { length: n - 1 },
          (_, t) => (actions[t + 1][ai] ?? 0) - (actions[t][ai] ?? 0),
        );
        const sDeltas = Array.from(
          { length: n - 1 },
          (_, t) => (states[t + 1][si] ?? 0) - (states[t][si] ?? 0),
        );
        const effN = aDeltas.length;
        if (effN < 4) continue;
        const aM = aDeltas.reduce((a, b) => a + b, 0) / effN;
        const sM = sDeltas.reduce((a, b) => a + b, 0) / effN;

        for (let li = 0; li < numLags; li++) {
          const lag = -maxLag + li;
          let sum = 0,
            aV = 0,
            sV = 0;
          for (let t = 0; t < effN; t++) {
            const sIdx = t + lag;
            if (sIdx < 0 || sIdx >= effN) continue;
            const a = aDeltas[t] - aM,
              s = sDeltas[sIdx] - sM;
            sum += a * s;
            aV += a * a;
            sV += s * s;
          }
          const d = Math.sqrt(aV * sV);
          if (d > 0) {
            corrSums[pi][li] += sum / d;
            corrCounts[pi][li]++;
          }
        }
      }
    }

    const avgCorrs = pairs.map((_, pi) =>
      Array.from({ length: numLags }, (_, li) =>
        corrCounts[pi][li] > 0 ? corrSums[pi][li] / corrCounts[pi][li] : 0,
      ),
    );

    const ccData = Array.from({ length: numLags }, (_, li) => {
      const lag = -maxLag + li;
      const vals = avgCorrs.map((pc) => pc[li]);
      return {
        lag,
        max: Math.max(...vals),
        mean: vals.reduce((a, b) => a + b, 0) / vals.length,
        min: Math.min(...vals),
      };
    });

    let meanPeakLag = 0,
      meanPeakCorr = -Infinity;
    let maxPeakLag = 0,
      maxPeakCorr = -Infinity;
    let minPeakLag = 0,
      minPeakCorr = -Infinity;
    for (const row of ccData) {
      if (row.max > maxPeakCorr) {
        maxPeakCorr = row.max;
        maxPeakLag = row.lag;
      }
      if (row.mean > meanPeakCorr) {
        meanPeakCorr = row.mean;
        meanPeakLag = row.lag;
      }
      if (row.min > minPeakCorr) {
        minPeakCorr = row.min;
        minPeakLag = row.lag;
      }
    }

    const perPairPeakLags = avgCorrs.map((pc) => {
      let best = -Infinity,
        bestLag = 0;
      for (let li = 0; li < pc.length; li++) {
        if (pc[li] > best) {
          best = pc[li];
          bestLag = -maxLag + li;
        }
      }
      return bestLag;
    });

    return {
      ccData,
      meanPeakLag,
      meanPeakCorr,
      maxPeakLag,
      maxPeakCorr,
      minPeakLag,
      minPeakCorr,
      lagRangeMin: Math.min(...perPairPeakLags),
      lagRangeMax: Math.max(...perPairPeakLags),
      numPairs: pairs.length,
    };
  })();

  return {
    actionNames,
    timeBins,
    variance,
    numEpisodes: episodeActions.length,
    lowMovementEpisodes,
    aggVelocity,
    aggAutocorrelation,
    speedDistribution,
    jerkyEpisodes,
    aggAlignment,
    scope,
    scopeEpisodes: inScope.length,
    requestedEpisodes: sampled.length,
    sampled: sampled.length < inScope.length,
  };
}

// Load only flatChartData for a specific episode (used by URDF viewer episode switching)
export async function loadEpisodeFlatChartData(
  repoId: string,
  version: string,
  info: DatasetMetadata,
  episodeId: number,
): Promise<Record<string, number>[]> {
  const normalizedVersion = normalizeDatasetVersion(version);
  if (!normalizedVersion) return [];
  if (!isDatasetV3(normalizedVersion)) {
    const result = await getEpisodeDataV2(
      repoId,
      normalizedVersion,
      info,
      episodeId,
    );
    return result.flatChartData;
  }
  const episodeMetadata = await loadEpisodeMetadataV3Simple(
    repoId,
    normalizedVersion,
    episodeId,
  );
  const { flatChartData } = await loadEpisodeDataV3(
    repoId,
    normalizedVersion,
    info,
    episodeMetadata,
  );
  return flatChartData;
}

// Safe wrapper for UI error display
export async function getEpisodeDataSafe(
  org: string,
  dataset: string,
  episodeId: number,
): Promise<{ data?: EpisodeData; error?: string }> {
  try {
    const data = await getEpisodeData(org, dataset, episodeId);
    return { data };
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    return { error: message || "Unknown error" };
  }
}
