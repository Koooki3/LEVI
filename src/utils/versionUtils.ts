// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
/**
 * Utility functions for checking dataset version compatibility
 */

import { authHeaders } from "./auth";

const DATASET_URL =
  process.env.DATASET_URL || "https://huggingface.co/datasets";

/**
 * Dataset information structure from info.json
 */
type FeatureInfo = {
  dtype: string;
  shape: number[];
  names: string[] | Record<string, unknown> | null;
  info?: Record<string, unknown>;
};

export interface DatasetInfo {
  codebase_version: string;
  robot_type: string | null;
  total_episodes: number;
  total_frames: number;
  total_tasks: number;
  chunks_size: number;
  data_files_size_in_mb: number;
  video_files_size_in_mb: number;
  fps: number;
  splits: Record<string, string>;
  data_path: string | null;
  video_path: string | null;
  features: Record<string, FeatureInfo>;
}

/** Supported LeRobot metadata versions, in newest-first display order. */
export const SUPPORTED_DATASET_VERSIONS = [
  "v3.1",
  "v3.0",
  "v2.1",
  "v2.0",
] as const;

/** Normalize version aliases emitted by different LeRobot writers. */
export function normalizeDatasetVersion(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = value
    .trim()
    .toLowerCase()
    .match(/^v?(\d+)\.(\d+)(?:\.\d+)?$/);
  if (!match) return null;
  const candidate = `v${Number(match[1])}.${Number(match[2])}`;
  return (SUPPORTED_DATASET_VERSIONS as readonly string[]).includes(candidate)
    ? candidate
    : null;
}

export function isDatasetV3(version: unknown): boolean {
  return normalizeDatasetVersion(version)?.startsWith("v3.") ?? false;
}

function asFiniteNumber(value: unknown, fallback: number): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "bigint") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function normalizeFeatureInfo(value: unknown): FeatureInfo | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  if (typeof raw.dtype !== "string") return null;
  const shape = Array.isArray(raw.shape)
    ? raw.shape
        .map((item) => asFiniteNumber(item, Number.NaN))
        .filter((item) => Number.isFinite(item))
        .map((item) => Math.max(0, Math.trunc(item)))
    : [];
  const names =
    raw.names === null ||
    Array.isArray(raw.names) ||
    typeof raw.names === "object"
      ? (raw.names as FeatureInfo["names"])
      : null;
  return {
    dtype: raw.dtype,
    shape,
    names,
    ...(raw.info && typeof raw.info === "object"
      ? { info: raw.info as Record<string, unknown> }
      : {}),
  };
}

function normalizeDatasetInfo(raw: unknown): DatasetInfo {
  if (!raw || typeof raw !== "object") {
    throw new Error("Dataset info.json must contain a JSON object");
  }
  const record = raw as Record<string, unknown>;
  if (
    !record.features ||
    typeof record.features !== "object" ||
    Array.isArray(record.features)
  ) {
    throw new Error(
      "Dataset info.json does not have the expected features structure",
    );
  }
  const features: Record<string, FeatureInfo> = {};
  for (const [key, value] of Object.entries(record.features)) {
    const feature = normalizeFeatureInfo(value);
    if (feature) features[key] = feature;
  }
  if (Object.keys(features).length === 0) {
    throw new Error("Dataset info.json does not contain any valid features");
  }
  const stringOrNull = (value: unknown): string | null =>
    typeof value === "string" && value.trim().length > 0 ? value.trim() : null;
  const splits =
    record.splits &&
    typeof record.splits === "object" &&
    !Array.isArray(record.splits)
      ? Object.fromEntries(
          Object.entries(record.splits).filter(
            ([, value]) => typeof value === "string",
          ),
        )
      : {};
  return {
    codebase_version:
      typeof record.codebase_version === "string"
        ? record.codebase_version.trim()
        : "",
    robot_type: stringOrNull(record.robot_type),
    total_episodes: Math.max(
      0,
      Math.trunc(asFiniteNumber(record.total_episodes, 0)),
    ),
    total_frames: Math.max(
      0,
      Math.trunc(asFiniteNumber(record.total_frames, 0)),
    ),
    total_tasks: Math.max(0, Math.trunc(asFiniteNumber(record.total_tasks, 0))),
    chunks_size: Math.max(
      1,
      Math.trunc(asFiniteNumber(record.chunks_size, 1000)),
    ),
    data_files_size_in_mb: Math.max(
      0,
      asFiniteNumber(record.data_files_size_in_mb, 0),
    ),
    video_files_size_in_mb: Math.max(
      0,
      asFiniteNumber(record.video_files_size_in_mb, 0),
    ),
    fps: Math.max(0, asFiniteNumber(record.fps, 0)),
    splits,
    data_path: stringOrNull(record.data_path),
    video_path: stringOrNull(record.video_path),
    features,
  };
}

// In-memory cache for dataset info (5 min TTL, bounded by MAX_CACHE_ENTRIES)
const datasetInfoCache = new Map<
  string,
  { data: DatasetInfo; expiry: number }
>();
const CACHE_TTL_MS = 5 * 60 * 1000;
const MAX_CACHE_ENTRIES = Math.max(
  8,
  parseInt(process.env.MAX_DATASET_INFO_CACHE_ENTRIES ?? "64", 10) || 64,
);

export function clearDatasetInfoCache(): void {
  datasetInfoCache.clear();
  datasetStatsCache.clear();
}

function pruneDatasetInfoCache(now: number) {
  // Remove expired entries first.
  for (const [key, value] of datasetInfoCache) {
    if (now >= value.expiry) {
      datasetInfoCache.delete(key);
    }
  }

  // Then cap overall cache size to prevent unbounded growth.
  while (datasetInfoCache.size > MAX_CACHE_ENTRIES) {
    const oldestKey = datasetInfoCache.keys().next().value;
    if (!oldestKey) break;
    datasetInfoCache.delete(oldestKey);
  }
}

export async function getDatasetInfo(repoId: string): Promise<DatasetInfo> {
  const now = Date.now();
  pruneDatasetInfoCache(now);

  const cached = datasetInfoCache.get(repoId);
  if (cached && now < cached.expiry) {
    // Keep insertion order fresh so the cache behaves closer to LRU.
    datasetInfoCache.delete(repoId);
    datasetInfoCache.set(repoId, cached);
    console.log(`[perf] getDatasetInfo cache HIT for ${repoId}`);
    return cached.data;
  }
  console.log(`[perf] getDatasetInfo cache MISS for ${repoId} — fetching`);

  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  try {
    const testUrl = buildVersionedUrl(repoId, "", "meta/info.json");

    const controller = new AbortController();
    timeoutId = setTimeout(() => controller.abort(), 10000);

    const response = await fetch(testUrl, {
      method: "GET",
      cache: "no-store",
      signal: controller.signal,
      headers: authHeaders(),
    });

    if (timeoutId) clearTimeout(timeoutId);
    timeoutId = undefined;

    if (!response.ok) {
      throw new Error(`Failed to fetch dataset info: ${response.status}`);
    }

    const data = await response.json();

    const normalized = normalizeDatasetInfo(data);

    datasetInfoCache.set(repoId, {
      data: normalized,
      expiry: Date.now() + CACHE_TTL_MS,
    });
    pruneDatasetInfoCache(Date.now());
    return normalized;
  } catch (error) {
    if (timeoutId) clearTimeout(timeoutId);
    if (error instanceof Error) {
      throw error;
    }
    throw new Error(
      `Dataset ${repoId} is not compatible with this visualizer. ` +
        "Failed to read dataset information from the main revision.",
    );
  }
}

// Per-feature statistics from meta/stats.json (min/max/mean/std and, when
// present, quantiles like q10/q90). Not every dataset ships this file, so the
// fetch is best-effort and returns null on any failure.
const datasetStatsCache = new Map<
  string,
  { data: Record<string, unknown> | null; expiry: number }
>();
function pruneDatasetStatsCache(now: number): void {
  for (const [key, value] of datasetStatsCache) {
    if (now >= value.expiry) datasetStatsCache.delete(key);
  }
  while (datasetStatsCache.size > MAX_CACHE_ENTRIES) {
    const oldestKey = datasetStatsCache.keys().next().value;
    if (!oldestKey) break;
    datasetStatsCache.delete(oldestKey);
  }
}
if (typeof window !== "undefined") {
  window.addEventListener("levi:hf-auth-changed", clearDatasetInfoCache);
}

export async function getDatasetStats(
  repoId: string,
): Promise<Record<string, unknown> | null> {
  const now = Date.now();
  pruneDatasetStatsCache(now);
  const cached = datasetStatsCache.get(repoId);
  if (cached && now < cached.expiry) {
    datasetStatsCache.delete(repoId);
    datasetStatsCache.set(repoId, cached);
    return cached.data;
  }

  let data: Record<string, unknown> | null = null;
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  try {
    const url = buildVersionedUrl(repoId, "", "meta/stats.json");
    const controller = new AbortController();
    timeoutId = setTimeout(() => controller.abort(), 10000);
    const response = await fetch(url, {
      method: "GET",
      cache: "no-store",
      signal: controller.signal,
      headers: authHeaders(),
    });
    if (timeoutId) clearTimeout(timeoutId);
    timeoutId = undefined;
    if (response.ok) {
      const json = await response.json();
      if (json && typeof json === "object") {
        data = json as Record<string, unknown>;
      }
    }
  } catch {
    if (timeoutId) clearTimeout(timeoutId);
    data = null;
  }

  datasetStatsCache.set(repoId, { data, expiry: Date.now() + CACHE_TTL_MS });
  pruneDatasetStatsCache(Date.now());
  return data;
}

/**
 * Returns both the validated version string and the dataset info in one call,
 * avoiding a duplicate info.json fetch.
 */
export async function getDatasetVersionAndInfo(
  repoId: string,
): Promise<{ version: string; info: DatasetInfo }> {
  const info = await getDatasetInfo(repoId);
  const rawVersion = info.codebase_version;
  if (!rawVersion) {
    throw new Error("Dataset info.json does not contain codebase_version");
  }
  const version = normalizeDatasetVersion(rawVersion);
  if (!version) {
    throw new Error(
      `Dataset ${repoId} has codebase version ${rawVersion}, which is not supported. ` +
        `This tool supports ${SUPPORTED_DATASET_VERSIONS.join(", ")}. ` +
        "Please use a compatible dataset version.",
    );
  }
  return { version, info: { ...info, codebase_version: version } };
}

export async function getDatasetVersion(repoId: string): Promise<string> {
  const { version } = await getDatasetVersionAndInfo(repoId);
  return version;
}

export function buildVersionedUrl(
  repoId: string,
  version: string,
  path: string,
): string {
  if (repoId.startsWith("local/")) {
    const prefix =
      typeof window === "undefined"
        ? `${process.env.LEVI_BACKEND_URL || "http://127.0.0.1:7861"}`
        : "";
    return `${prefix}/api/levi/files/${repoId.slice(6)}/${path}`;
  }
  return `${DATASET_URL}/${repoId}/resolve/main/${path}`;
}
