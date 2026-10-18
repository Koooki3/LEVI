import {
  asyncBufferFromUrl,
  parquetMetadataAsync,
  parquetRead,
  parquetReadObjects,
  type AsyncBuffer,
  type FileMetaData,
} from "hyparquet";
import { authHeaders } from "./auth";

export interface DatasetMetadata {
  codebase_version: string;
  robot_type: string;
  total_episodes: number;
  total_frames: number;
  total_tasks: number;
  total_videos: number;
  total_chunks: number;
  chunks_size: number;
  fps: number;
  splits: Record<string, string>;
  data_path: string;
  video_path: string;
  features: Record<
    string,
    {
      dtype: string;
      shape: number[];
      names: string[] | Record<string, unknown> | null;
      info?: Record<string, unknown>;
    }
  >;
}

export async function fetchJson<T>(url: string): Promise<T> {
  const res = await fetch(url, {
    cache: "no-store",
    headers: authHeaders(url),
  });
  if (!res.ok) {
    throw new Error(
      `Failed to fetch JSON ${url}: ${res.status} ${res.statusText}`,
    );
  }
  return res.json() as Promise<T>;
}

export function formatStringWithVars(
  format: string,
  vars: Record<string, string | number>,
): string {
  return format.replace(/{(\w+)(?::\d+d)?}/g, (_, key) => String(vars[key]));
}

// ---------------------------------------------------------------------------
// Parquet reading: bounded memory
//
// Three limits keep a long browsing session (or one huge file) from eating the
// tab's memory silently:
//   1. A byte-budget LRU over the bytes actually held per file. Small metadata
//      files (episodes, tasks, progress) and large data files draw on separate
//      budgets, so a big data file cannot push the metadata out.
//   2. A row-group index per file (built from the footer), so an episode's rows
//      are located and read as a row range, not by reading the whole file.
//   3. A cap on whole-file reads: above it the read fails with a clear
//      ParquetTooLargeError instead of decoding the file into memory.
// ---------------------------------------------------------------------------
type ParquetFile = ArrayBuffer | AsyncBuffer;

const MIB = 1024 * 1024;

function envNumber(name: string, fallback: number, minimum: number): number {
  const parsed = Number.parseFloat(process.env[name] ?? "");
  return Number.isFinite(parsed) && parsed >= minimum ? parsed : fallback;
}

export interface ParquetLimits {
  /** Budget for cached bytes of small (metadata) files. */
  metaCacheBytes: number;
  /** Budget for cached bytes of large (data) files. */
  dataCacheBytes: number;
  /** Most files held at once, whatever their size. */
  maxEntries: number;
  /** Files up to this size count as metadata. */
  metaFileBytes: number;
  /** Largest file (or row-group selection) a full read may decode. */
  fullReadBytes: number;
}

export const parquetLimits: ParquetLimits = {
  metaCacheBytes: envNumber("MAX_PARQUET_META_CACHE_MB", 32, 1) * MIB,
  dataCacheBytes: envNumber("MAX_PARQUET_CACHE_MB", 192, 1) * MIB,
  maxEntries: Math.floor(envNumber("MAX_PARQUET_CACHE_ENTRIES", 64, 8)),
  metaFileBytes: 4 * MIB,
  fullReadBytes: envNumber("MAX_PARQUET_FULL_READ_MB", 128, 1) * MIB,
};

/** A read that would load more of a file into memory than the limit allows. */
export class ParquetTooLargeError extends Error {
  readonly bytes: number;
  readonly limit: number;
  constructor(what: string, bytes: number, limit: number) {
    super(
      `${what} needs ${
        Number.isFinite(bytes)
          ? `about ${Math.round(bytes / MIB)} MiB`
          : "an unknown size"
      }, above the ${Math.round(limit / MIB)} MiB limit for reading a ` +
        "Parquet file into memory; it was not loaded.",
    );
    this.name = "ParquetTooLargeError";
    this.bytes = bytes;
    this.limit = limit;
  }
}

type SliceKind = "meta" | "data";

class CacheEntry {
  constructor(
    readonly url: string,
    readonly generation: number,
  ) {}
  readonly slices = new Map<string, Promise<ArrayBuffer>>();
  bytes = 0;
  evicted = false;
  /** Bytes reported while the file was still being opened. */
  early = 0;
  registered = false;
  buffer!: AsyncBuffer;
  kind: SliceKind = "meta";
}

export interface OpenHooks {
  /** The server ignored Range and the whole body is now held in memory. */
  onHeldBytes: (bytes: number) => void;
}

/**
 * Per-URL AsyncBuffer cache bounded in bytes, least recently used evicted
 * first. Slices are cached individually and counted when they arrive.
 */
export class ParquetBufferCache {
  readonly limits: ParquetLimits;
  private readonly entries = new Map<string, CacheEntry>();
  private readonly inflight = new Map<
    string,
    { generation: number; promise: Promise<AsyncBuffer> }
  >();
  private generation = 0;
  private readonly held: Record<SliceKind, number> = { meta: 0, data: 0 };

  constructor(limits: ParquetLimits) {
    this.limits = limits;
  }

  stats() {
    return {
      entries: this.entries.size,
      metaBytes: this.held.meta,
      dataBytes: this.held.data,
    };
  }

  clear(): void {
    this.generation += 1;
    for (const entry of this.entries.values()) this.release(entry);
    this.entries.clear();
    this.inflight.clear();
    this.held.meta = 0;
    this.held.data = 0;
  }

  async get(
    url: string,
    open: (hooks: OpenHooks) => Promise<AsyncBuffer>,
  ): Promise<AsyncBuffer> {
    const hit = this.entries.get(url);
    if (hit) {
      this.entries.delete(url);
      this.entries.set(url, hit); // most recently used
      return hit.buffer;
    }
    const generation = this.generation;
    const pending = this.inflight.get(url);
    if (pending?.generation === generation) return pending.promise;

    const entry = new CacheEntry(url, generation);
    const promise = (async () => {
      const source = await open({
        onHeldBytes: (bytes) =>
          entry.registered ? this.add(entry, bytes) : (entry.early += bytes),
      });
      entry.kind =
        source.byteLength <= this.limits.metaFileBytes ? "meta" : "data";
      entry.buffer = this.wrap(source, entry);
      entry.registered = true;
      if (generation === this.generation) {
        this.entries.set(url, entry);
        this.add(entry, entry.early);
        this.trim(entry);
      } else {
        this.release(entry);
      }
      return entry.buffer;
    })();
    this.inflight.set(url, { generation, promise });
    try {
      return await promise;
    } finally {
      if (this.inflight.get(url)?.promise === promise)
        this.inflight.delete(url);
    }
  }

  private budget(kind: SliceKind): number {
    return kind === "meta"
      ? this.limits.metaCacheBytes
      : this.limits.dataCacheBytes;
  }

  private release(entry: CacheEntry): void {
    entry.evicted = true;
    entry.slices.clear();
    entry.bytes = 0;
  }

  private evict(url: string, entry: CacheEntry): void {
    this.entries.delete(url);
    this.held[entry.kind] -= entry.bytes;
    this.release(entry);
  }

  private add(entry: CacheEntry, bytes: number): void {
    if (entry.generation !== this.generation) return; // cleared meanwhile
    if (entry.evicted || bytes <= 0) return;
    entry.bytes += bytes;
    this.held[entry.kind] += bytes;
    this.trim(entry);
  }

  /** Evict least-recently-used files until both budgets and the file-count cap hold. */
  private trim(current: CacheEntry): void {
    const over = (kind: SliceKind) => this.held[kind] > this.budget(kind);
    for (const kind of ["meta", "data"] as const) {
      while (over(kind)) {
        const victim = [...this.entries].find(
          ([, e]) => e !== current && e.kind === kind && e.bytes > 0,
        );
        if (!victim) break;
        this.evict(victim[0], victim[1]);
      }
      if (over(kind) && current.kind === kind && !current.evicted) {
        // This file alone is beyond the budget: leave the table (the caller
        // keeps reading through its buffer, uncached; the next get reopens),
        // so the counts are exactly what the cache holds.
        if (this.entries.get(current.url) === current) {
          this.evict(current.url, current);
        }
      }
    }
    while (this.entries.size > this.limits.maxEntries) {
      const oldest = [...this.entries].find(([, e]) => e !== current);
      if (!oldest) break;
      this.evict(oldest[0], oldest[1]);
    }
  }

  private wrap(source: AsyncBuffer, entry: CacheEntry): AsyncBuffer {
    const whole = source.byteLength < 512 * 1024;
    return {
      byteLength: source.byteLength,
      slice: (start: number, end?: number) => {
        // Small files: one request for the whole file, then slice from memory.
        const [from, to] = whole ? [0, source.byteLength] : [start, end];
        const key = `${from}-${to ?? ""}`;
        const size = (to ?? source.byteLength) - from;
        if (entry.evicted || size > this.budget(entry.kind)) {
          return source.slice(start, end);
        }
        let cached = entry.slices.get(key);
        if (!cached) {
          const request = Promise.resolve(source.slice(from, to));
          cached = request;
          entry.slices.set(key, request);
          request.then(
            (buffer) => {
              if (entry.slices.get(key) === request)
                this.add(entry, buffer.byteLength);
            },
            () => {
              if (entry.slices.get(key) === request) entry.slices.delete(key);
            },
          );
        }
        return whole
          ? cached.then((buffer) => buffer.slice(start, end))
          : cached;
      },
    };
  }
}

const parquetCache = new ParquetBufferCache(parquetLimits);
const parquetFileIndexes = new Map<string, ParquetFileIndex>();
const MAX_PARQUET_INDEX_ENTRIES = 256;

export function clearParquetFileCache(): void {
  parquetCache.clear();
  parquetFileIndexes.clear();
}

export function parquetCacheStats() {
  return { ...parquetCache.stats(), indexes: parquetFileIndexes.size };
}

if (typeof window !== "undefined") {
  window.addEventListener("levi:hf-auth-changed", clearParquetFileCache);
}

/**
 * fetch() that refuses to download a huge body when the server ignores Range.
 * The size comes from the 200's Content-Length or from the HEAD that opened
 * the file (whichever is larger: a compressed body reports the smaller one);
 * with neither, the body is refused rather than trusted.
 */
function rangeGuardedFetch(hooks: OpenHooks): typeof fetch {
  let headLength = Number.NaN;
  let reported = false;
  const guarded = async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    const response = await fetch(input, init);
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "HEAD" && response.ok) {
      const length = Number.parseInt(
        response.headers.get("Content-Length") ?? "",
        10,
      );
      if (Number.isFinite(length)) headLength = length;
    } else if (
      response.status === 200 &&
      new Headers(init?.headers).has("Range")
    ) {
      const own = Number.parseInt(
        response.headers.get("Content-Length") ?? "",
        10,
      );
      const length = Math.max(
        Number.isFinite(own) ? own : 0,
        Number.isFinite(headLength) ? headLength : 0,
      );
      const known = Number.isFinite(own) || Number.isFinite(headLength);
      if (!known || length > parquetLimits.fullReadBytes) {
        void response.body?.cancel();
        throw new ParquetTooLargeError(
          known
            ? "This server does not support range requests; the file"
            : "This server does not support range requests and did not say how large the file is; it",
          known ? length : Number.POSITIVE_INFINITY,
          parquetLimits.fullReadBytes,
        );
      }
      // hyparquet now keeps the whole body for the life of the buffer; count
      // it once however many slices were in flight.
      if (!reported) {
        reported = true;
        hooks.onHeldBytes(length);
      }
    }
    return response;
  };
  return guarded as typeof fetch;
}

export async function fetchParquetFile(url: string): Promise<ParquetFile> {
  return parquetCache.get(url, async (hooks) =>
    asyncBufferFromUrl({
      url,
      requestInit: { cache: "no-store", headers: authHeaders(url) },
      fetch: rangeGuardedFetch(hooks),
    }),
  );
}

function assertFullReadAllowed(file: ParquetFile, what: string): void {
  if (file.byteLength > parquetLimits.fullReadBytes) {
    throw new ParquetTooLargeError(
      what,
      file.byteLength,
      parquetLimits.fullReadBytes,
    );
  }
}

// ---------------------------------------------------------------------------
// Row-group index: which rows live in which row group, and how many bytes
// ---------------------------------------------------------------------------
export interface ParquetRowGroupSpan {
  /** First row of the group, counted from the start of the file. */
  start: number;
  /** One past the last row. */
  end: number;
  /** Compressed bytes of the whole group. */
  bytes: number;
  /** Compressed bytes per top-level column. */
  columnBytes: Record<string, number>;
}

export interface ParquetFileIndex {
  byteLength: number;
  numRows: number;
  rowGroups: ParquetRowGroupSpan[];
  /**
   * Value of the `index` column in the file's first row (v3 data files are
   * contiguous in the global frame index). `undefined` until looked up,
   * `null` when the file has no such column.
   */
  firstIndex?: number | null;
}

export function buildParquetFileIndex(
  metadata: FileMetaData,
  byteLength: number,
): ParquetFileIndex {
  let start = 0;
  const rowGroups: ParquetRowGroupSpan[] = [];
  for (const group of metadata.row_groups) {
    const rows = Number(group.num_rows);
    const columnBytes: Record<string, number> = {};
    let bytes = 0;
    for (const column of group.columns) {
      const size = Number(column.meta_data?.total_compressed_size ?? 0);
      const name = column.meta_data?.path_in_schema?.[0];
      bytes += size;
      if (name !== undefined)
        columnBytes[name] = (columnBytes[name] ?? 0) + size;
    }
    if (bytes === 0) bytes = Number(group.total_byte_size ?? 0);
    rowGroups.push({ start, end: start + rows, bytes, columnBytes });
    start += rows;
  }
  return { byteLength, numRows: start, rowGroups };
}

export interface ParquetRangePlan {
  rowStart: number;
  rowEnd: number;
  /** Row groups touched, first and last (inclusive); -1 when none. */
  firstGroup: number;
  lastGroup: number;
  /** Compressed bytes that reading these rows and columns must fetch. */
  bytes: number;
}

export function planParquetRowRange(
  index: ParquetFileIndex,
  rowStart: number,
  rowEnd: number,
  columns: string[] = [],
): ParquetRangePlan {
  const from = Math.max(0, Math.min(rowStart, index.numRows));
  const to = Math.max(from, Math.min(rowEnd, index.numRows));
  let firstGroup = -1;
  let lastGroup = -1;
  let bytes = 0;
  index.rowGroups.forEach((group, i) => {
    if (group.end <= from || group.start >= to) return;
    if (firstGroup < 0) firstGroup = i;
    lastGroup = i;
    const named = columns.filter((c) => c in group.columnBytes);
    bytes +=
      named.length > 0
        ? named.reduce((sum, c) => sum + group.columnBytes[c], 0)
        : group.bytes;
  });
  return { rowStart: from, rowEnd: to, firstGroup, lastGroup, bytes };
}

export async function getParquetFileIndex(
  url: string,
  file: AsyncBuffer,
): Promise<ParquetFileIndex> {
  const known = parquetFileIndexes.get(url);
  if (known && known.byteLength === file.byteLength) {
    parquetFileIndexes.delete(url);
    parquetFileIndexes.set(url, known);
    return known;
  }
  const index = buildParquetFileIndex(
    await parquetMetadataAsync(file),
    file.byteLength,
  );
  parquetFileIndexes.set(url, index);
  while (parquetFileIndexes.size > MAX_PARQUET_INDEX_ENTRIES) {
    const oldest = parquetFileIndexes.keys().next().value;
    if (oldest === undefined) break;
    parquetFileIndexes.delete(oldest);
  }
  return index;
}

/** A requested episode range cannot be proven from the file's real index. */
export class ParquetIndexRangeError extends Error {
  constructor(detail: string) {
    super(
      `Parquet frame range unavailable: ${detail}; no partial episode was used.`,
    );
    this.name = "ParquetIndexRangeError";
  }
}

export function requireParquetGlobalIndex(value: unknown): number {
  const number = typeof value === "bigint" ? Number(value) : value;
  if (
    typeof number !== "number" ||
    !Number.isSafeInteger(number) ||
    number < 0
  ) {
    throw new ParquetIndexRangeError("missing or invalid global frame index");
  }
  return number;
}

function checkGlobalRange(fromIndex: number, toIndex: number): void {
  if (typeof fromIndex !== "number" || typeof toIndex !== "number") {
    throw new ParquetIndexRangeError(
      "range bounds must be safe integer numbers",
    );
  }
  requireParquetGlobalIndex(fromIndex);
  requireParquetGlobalIndex(toIndex);
  if (toIndex < fromIndex) {
    throw new ParquetIndexRangeError("range end precedes its start");
  }
}

/** Select by real index, never by row position; unrelated indexed rows are OK. */
export function selectParquetRowsByGlobalIndex(
  rows: Record<string, unknown>[],
  fromIndex: number,
  toIndex: number,
  indexColumn = "index",
): Record<string, unknown>[] {
  checkGlobalRange(fromIndex, toIndex);
  if (fromIndex === toIndex) return [];
  const selected: { index: number; row: Record<string, unknown> }[] = [];
  for (const row of rows) {
    // A row without a reliable index cannot be classified as outside the range.
    const index = requireParquetGlobalIndex(row[indexColumn]);
    if (index >= fromIndex && index < toIndex) selected.push({ index, row });
  }
  selected.sort((a, b) => a.index - b.index);
  if (
    selected.length !== toIndex - fromIndex ||
    selected.some((item, offset) => item.index !== fromIndex + offset)
  ) {
    throw new ParquetIndexRangeError(
      `incomplete or duplicate indexes in [${fromIndex}, ${toIndex})`,
    );
  }
  return selected.map((item) => item.row);
}

function assertRangeReadAllowed(plan: ParquetRangePlan, what: string): void {
  if (
    !Number.isFinite(plan.bytes) ||
    plan.bytes > parquetLimits.fullReadBytes
  ) {
    throw new ParquetTooLargeError(
      what,
      plan.bytes,
      parquetLimits.fullReadBytes,
    );
  }
}

/**
 * A complete global [fromIndex, toIndex) range, or null for a capped fallback.
 * The local offset planner may clip; this API must not. Suspect cached indexes
 * are rebuilt from the current buffer at most once, including same-size rewrites.
 * An explicitly empty request returns [] without reading the buffer.
 */
export async function readParquetRowsByGlobalIndex(
  url: string,
  file: AsyncBuffer,
  columns: string[],
  fromIndex: number,
  toIndex: number,
  indexColumn = "index",
): Promise<Record<string, unknown>[] | null> {
  checkGlobalRange(fromIndex, toIndex);
  if (fromIndex === toIndex) return [];
  // Verification is mandatory even when the caller did not project index.
  const projected =
    columns.length === 0 ? [] : [...new Set([...columns, indexColumn])];
  for (let attempt = 0; attempt < 2; attempt++) {
    const index = await getParquetFileIndex(url, file);
    if (Number.isSafeInteger(index.numRows) && index.numRows > 0) {
      if (index.firstIndex === undefined) {
        assertRangeReadAllowed(
          planParquetRowRange(index, 0, 1, [indexColumn]),
          `Locating global indexes of ${url}`,
        );
        const first = await readParquetAsObjects(file, [indexColumn], {
          rowStart: 0,
          rowEnd: 1,
        });
        try {
          index.firstIndex = requireParquetGlobalIndex(first[0]?.[indexColumn]);
        } catch (error) {
          if (!(error instanceof ParquetIndexRangeError)) throw error;
          index.firstIndex = null;
        }
      }
      if (index.firstIndex !== null && index.firstIndex !== undefined) {
        const localFrom = fromIndex - index.firstIndex;
        const localTo = toIndex - index.firstIndex;
        if (localFrom >= 0 && localTo <= index.numRows) {
          const plan = planParquetRowRange(
            index,
            localFrom,
            localTo,
            projected,
          );
          if (plan.firstGroup >= 0) {
            assertRangeReadAllowed(
              plan,
              `Rows ${fromIndex}-${toIndex} of ${url}`,
            );
            const rows = await readParquetAsObjects(file, projected, {
              rowStart: plan.rowStart,
              rowEnd: plan.rowEnd,
            });
            try {
              if (rows.length !== toIndex - fromIndex) {
                throw new ParquetIndexRangeError("short row-range read");
              }
              const verified = selectParquetRowsByGlobalIndex(
                rows,
                fromIndex,
                toIndex,
                indexColumn,
              );
              if (columns.length === 0 || columns.includes(indexColumn))
                return verified;
              return verified.map((row) => {
                const copy = { ...row };
                delete copy[indexColumn];
                return copy;
              });
            } catch (error) {
              if (!(error instanceof ParquetIndexRangeError)) throw error;
            }
          }
        }
      }
    }
    // The index can outlive its cached bytes. Retry once using this buffer,
    // not the clipped offsets or first index from the old file.
    parquetFileIndexes.delete(url);
  }
  return null;
}

// Read specific columns from the Parquet file
export async function readParquetColumn(
  fileBuffer: ParquetFile,
  columns: string[],
  options?: { rowStart?: number; rowEnd?: number },
): Promise<unknown[][]> {
  if (options?.rowStart === undefined && options?.rowEnd === undefined) {
    assertFullReadAllowed(fileBuffer, "Reading this Parquet file");
  }
  return new Promise((resolve, reject) => {
    try {
      parquetRead({
        file: fileBuffer,
        columns: columns.length > 0 ? columns : undefined,
        rowStart: options?.rowStart,
        rowEnd: options?.rowEnd,
        onComplete: (data: unknown[][]) => {
          resolve(data);
        },
      });
    } catch (error) {
      reject(error);
    }
  });
}

export async function readParquetAsObjects(
  fileBuffer: ParquetFile,
  columns: string[] = [],
  options?: { rowStart?: number; rowEnd?: number },
): Promise<Record<string, unknown>[]> {
  if (options?.rowStart === undefined && options?.rowEnd === undefined) {
    assertFullReadAllowed(fileBuffer, "Reading this Parquet file");
  }
  return parquetReadObjects({
    file: fileBuffer,
    columns: columns.length > 0 ? columns : undefined,
    rowStart: options?.rowStart,
    rowEnd: options?.rowEnd,
  }) as Promise<Record<string, unknown>[]>;
}

// Convert a 2D array to a CSV string
export function arrayToCSV(data: (number | string)[][]): string {
  return data.map((row) => row.join(",")).join("\n");
}

type ColumnInfo = { key: string; value: string[] };

export function getRows(currentFrameData: unknown[], columns: ColumnInfo[]) {
  if (!currentFrameData || currentFrameData.length === 0) {
    return [];
  }

  const rows: Array<Array<{ isNull: true } | unknown>> = [];
  const nRows = Math.max(...columns.map((column) => column.value.length));
  let rowIndex = 0;

  while (rowIndex < nRows) {
    const row: Array<{ isNull: true } | unknown> = [];
    // number of states may NOT match number of actions. In this case, we null-pad the 2D array
    const nullCell = { isNull: true };
    // row consists of [state value, action value]
    let idx = rowIndex;

    for (const column of columns) {
      const nColumn = column.value.length;
      row.push(rowIndex < nColumn ? currentFrameData[idx] : nullCell);
      idx += nColumn; // because currentFrameData = [state0, state1, ..., stateN, action0, action1, ..., actionN]
    }

    rowIndex += 1;
    rows.push(row);
  }

  return rows;
}
