import { afterEach, describe, expect, test } from "bun:test";
import type { AsyncBuffer } from "hyparquet";
import {
  ParquetBufferCache,
  ParquetTooLargeError,
  buildParquetFileIndex,
  fetchParquetFile,
  clearParquetFileCache,
  getParquetFileIndex,
  parquetCacheStats,
  parquetLimits,
  planParquetRowRange,
  readParquetAsObjects,
  readParquetRowsByGlobalIndex,
  type ParquetLimits,
} from "@/utils/parquetUtils";

// 30 rows in 3 row groups of 10; columns index (int64, 100..129) and value
// (float64, 0.5 * row). Written with pyarrow, snappy.
const FIXTURE_B64 =
  "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";

// Two uncompressed files of the same length: 30 rows, index 100..129 and
// index 200..229 (a file rewritten in place under the same URL).
const REWRITE_A_B64 =
  "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";
const REWRITE_B_B64 =
  "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";

function bufferOf(b64: string): AsyncBuffer {
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const buffer = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
  return { byteLength: buffer.byteLength, slice: (a, b) => buffer.slice(a, b) };
}

function fixtureBuffer(): AsyncBuffer & { slices: number } {
  const bytes = Uint8Array.from(atob(FIXTURE_B64), (c) => c.charCodeAt(0));
  const buffer = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
  const out = {
    byteLength: buffer.byteLength,
    slices: 0,
    slice(start: number, end?: number) {
      out.slices += 1;
      return buffer.slice(start, end);
    },
  };
  return out;
}

/** A file of `size` bytes whose slices are zero-filled; counts the requests. */
function fakeFile(size: number) {
  const file = {
    byteLength: size,
    requests: 0,
    slice(start: number, end?: number) {
      file.requests += 1;
      return new ArrayBuffer((end ?? size) - start);
    },
  };
  return file;
}

const KIB = 1024;

function limits(over: Partial<ParquetLimits> = {}): ParquetLimits {
  return {
    metaCacheBytes: 100 * KIB,
    dataCacheBytes: 1000 * KIB,
    maxEntries: 64,
    metaFileBytes: 600 * KIB,
    fullReadBytes: 5000 * KIB,
    ...over,
  };
}

describe("ParquetBufferCache byte budget", () => {
  test("a repeated slice is served from memory", async () => {
    const cache = new ParquetBufferCache(limits());
    const file = fakeFile(900 * KIB);
    const buffer = await cache.get("a", async () => file);
    await buffer.slice(0, 100 * KIB);
    await buffer.slice(0, 100 * KIB);
    expect(file.requests).toBe(1);
    expect(cache.stats().dataBytes).toBe(100 * KIB);
  });

  test("least recently used data file is evicted when the budget is exceeded", async () => {
    const cache = new ParquetBufferCache(limits({ dataCacheBytes: 250 * KIB }));
    const files = new Map<string, ReturnType<typeof fakeFile>>();
    const open = (name: string) => async () => {
      const f = fakeFile(900 * KIB);
      files.set(name, f);
      return f;
    };
    const a = await cache.get("a", open("a"));
    const b = await cache.get("b", open("b"));
    await a.slice(0, 100 * KIB);
    await b.slice(0, 100 * KIB);
    // Touch a, so b is the least recently used.
    await cache.get("a", open("a2"));
    const c = await cache.get("c", open("c"));
    await c.slice(0, 100 * KIB);
    expect(cache.stats().dataBytes).toBe(200 * KIB);
    expect(cache.stats().entries).toBe(2);
    // a survived (no reopen), b was evicted (reopened).
    expect(files.has("a2")).toBe(false);
    await cache.get("b", open("b2"));
    expect(files.has("b2")).toBe(true);
  });

  test("metadata files have their own budget and are not pushed out by data", async () => {
    const cache = new ParquetBufferCache(
      limits({ dataCacheBytes: 150 * KIB, metaCacheBytes: 100 * KIB }),
    );
    const meta = fakeFile(50 * KIB);
    const big = fakeFile(900 * KIB);
    const m = await cache.get("meta", async () => meta);
    await m.slice(0, 50 * KIB);
    const d1 = await cache.get("d1", async () => big);
    const d2 = await cache.get("d2", async () => fakeFile(900 * KIB));
    await d1.slice(0, 100 * KIB);
    await d2.slice(0, 100 * KIB); // evicts d1, never meta
    const stats = cache.stats();
    expect(stats.metaBytes).toBe(50 * KIB);
    expect(stats.dataBytes).toBe(100 * KIB);
    expect(stats.entries).toBe(2);
    const again = await cache.get("meta", async () => {
      throw new Error("metadata should still be cached");
    });
    expect(again).toBe(m);
  });

  test("a file larger than the whole budget is read but not cached", async () => {
    const cache = new ParquetBufferCache(limits({ dataCacheBytes: 100 * KIB }));
    const file = fakeFile(900 * KIB);
    const buffer = await cache.get("big", async () => file);
    await buffer.slice(0, 80 * KIB);
    await buffer.slice(100 * KIB, 400 * KIB); // bigger than the budget
    expect((await buffer.slice(0, 80 * KIB)).byteLength).toBe(80 * KIB);
    expect(cache.stats().dataBytes).toBeLessThanOrEqual(100 * KIB);
  });

  test("the budget holds after a run of reads on many files", async () => {
    const cache = new ParquetBufferCache(
      limits({ dataCacheBytes: 300 * KIB, maxEntries: 8 }),
    );
    for (let i = 0; i < 40; i++) {
      const b = await cache.get(`f${i}`, async () => fakeFile(900 * KIB));
      await b.slice(0, 90 * KIB);
      await b.slice(90 * KIB, 150 * KIB);
      expect(cache.stats().dataBytes).toBeLessThanOrEqual(300 * KIB);
      expect(cache.stats().entries).toBeLessThanOrEqual(8);
    }
  });

  test("the entry cap evicts the oldest file", async () => {
    const cache = new ParquetBufferCache(limits({ maxEntries: 3 }));
    for (const name of ["a", "b", "c", "d"]) {
      await cache.get(name, async () => fakeFile(10 * KIB));
    }
    expect(cache.stats().entries).toBe(3);
    let reopened = false;
    await cache.get("a", async () => {
      reopened = true;
      return fakeFile(10 * KIB);
    });
    expect(reopened).toBe(true);
  });

  test("concurrent opens of one URL share a single request", async () => {
    const cache = new ParquetBufferCache(limits());
    let opens = 0;
    const open = async () => {
      opens += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return fakeFile(10 * KIB);
    };
    const [x, y] = await Promise.all([
      cache.get("u", open),
      cache.get("u", open),
    ]);
    expect(opens).toBe(1);
    expect(x).toBe(y);
  });

  test("clear() drops everything, and an open in flight is not cached afterwards", async () => {
    const cache = new ParquetBufferCache(limits());
    const warm = await cache.get("warm", async () => fakeFile(900 * KIB));
    await warm.slice(0, 50 * KIB);
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const slow = cache.get("slow", async () => {
      await gate;
      return fakeFile(900 * KIB);
    });
    cache.clear();
    release();
    const buffer = await slow;
    await buffer.slice(0, 10 * KIB);
    expect(cache.stats()).toEqual({ entries: 0, metaBytes: 0, dataBytes: 0 });
    // The evicted buffer still reads (uncached), its bytes are not counted.
    expect((await warm.slice(0, 50 * KIB)).byteLength).toBe(50 * KIB);
    expect(cache.stats().dataBytes).toBe(0);
  });

  test("a failed slice is not cached", async () => {
    const cache = new ParquetBufferCache(limits());
    let fail = true;
    const file = {
      byteLength: 900 * KIB,
      slice: async (start: number, end?: number) => {
        if (fail) throw new Error("network");
        return new ArrayBuffer((end ?? 900 * KIB) - start);
      },
    };
    const buffer = await cache.get("x", async () => file);
    await expect(buffer.slice(0, 10 * KIB)).rejects.toThrow("network");
    fail = false;
    expect((await buffer.slice(0, 10 * KIB)).byteLength).toBe(10 * KIB);
  });

  test("bytes the server forces us to hold count against the budget", async () => {
    const cache = new ParquetBufferCache(limits({ dataCacheBytes: 500 * KIB }));
    await cache.get("held", async (hooks) => {
      hooks.onHeldBytes(300 * KIB);
      return fakeFile(900 * KIB);
    });
    expect(cache.stats()).toMatchObject({ metaBytes: 0, dataBytes: 300 * KIB });
  });
});

describe("a file that outgrows its budget", () => {
  test("is dropped from the table, so the statistics match what is held", async () => {
    const cache = new ParquetBufferCache(limits({ dataCacheBytes: 500 * KIB }));
    let opens = 0;
    const open = async (hooks: { onHeldBytes: (n: number) => void }) => {
      opens += 1;
      hooks.onHeldBytes(400 * KIB); // the server forced a whole body on us
      return fakeFile(900 * KIB);
    };
    const buffer = await cache.get("held", open);
    expect(cache.stats()).toMatchObject({ entries: 1, dataBytes: 400 * KIB });
    // Slices push it past the budget: the entry leaves the table entirely
    // (its held body is no longer ours to count), still readable by the caller.
    await buffer.slice(0, 80 * KIB);
    await buffer.slice(100 * KIB, 180 * KIB);
    expect(cache.stats()).toEqual({ entries: 0, metaBytes: 0, dataBytes: 0 });
    expect((await buffer.slice(0, 10 * KIB)).byteLength).toBe(10 * KIB);
    expect(cache.stats().dataBytes).toBe(0);
    await cache.get("held", open);
    expect(opens).toBe(2); // reopened, not served from a half-counted entry
  });

  test("a held body alone above the budget never enters the table", async () => {
    const cache = new ParquetBufferCache(limits({ dataCacheBytes: 100 * KIB }));
    await cache.get("held", async (hooks) => {
      hooks.onHeldBytes(300 * KIB);
      return fakeFile(900 * KIB);
    });
    expect(cache.stats()).toEqual({ entries: 0, metaBytes: 0, dataBytes: 0 });
  });
});

describe("row-group index", () => {
  test("indexes row groups and plans a row range", async () => {
    const file = fixtureBuffer();
    const index = await getParquetFileIndex("fixture", file);
    expect(index.numRows).toBe(30);
    expect(index.rowGroups.map((g) => [g.start, g.end])).toEqual([
      [0, 10],
      [10, 20],
      [20, 30],
    ]);
    const plan = planParquetRowRange(index, 12, 25, ["value"]);
    expect([plan.firstGroup, plan.lastGroup]).toEqual([1, 2]);
    expect(plan.bytes).toBe(
      index.rowGroups[1].columnBytes.value +
        index.rowGroups[2].columnBytes.value,
    );
    expect(planParquetRowRange(index, 40, 50).firstGroup).toBe(-1);
    expect(planParquetRowRange(index, 0, 1000).lastGroup).toBe(2);
  });

  test("buildParquetFileIndex falls back to row-group size when columns have none", () => {
    const index = buildParquetFileIndex(
      {
        row_groups: [
          { num_rows: 5n, total_byte_size: 77n, columns: [] },
          { num_rows: 5n, total_byte_size: 11n, columns: [] },
        ],
      } as never,
      1000,
    );
    expect(index.rowGroups.map((g) => g.bytes)).toEqual([77, 11]);
    expect(index.numRows).toBe(10);
  });

  test("reads one episode as global-index range through the row groups", async () => {
    clearParquetFileCache();
    const file = fixtureBuffer();
    const rows = await readParquetRowsByGlobalIndex(
      "fixture-range",
      file,
      ["index", "value"],
      112,
      118,
    );
    expect(rows?.map((r) => Number(r.index))).toEqual([
      112, 113, 114, 115, 116, 117,
    ]);
    expect(rows?.[0].value).toBe(6);
    // The first-row lookup is remembered: a second episode needs no preview read.
    const before = file.slices;
    const next = await readParquetRowsByGlobalIndex(
      "fixture-range",
      file,
      ["index", "value"],
      120,
      123,
    );
    expect(next?.map((r) => Number(r.index))).toEqual([120, 121, 122]);
    expect(file.slices - before).toBeGreaterThan(0); // data fetched
    expect(
      await readParquetRowsByGlobalIndex(
        "fixture-range",
        file,
        ["index"],
        500,
        510,
      ),
    ).toEqual([]);
  });

  test("a file rewritten under the same URL and length is not read through a stale index", async () => {
    clearParquetFileCache();
    const before = bufferOf(REWRITE_A_B64);
    const after = bufferOf(REWRITE_B_B64);
    expect(after.byteLength).toBe(before.byteLength);
    const url = "fixture-rewritten";
    const first = await readParquetRowsByGlobalIndex(url, before, ["index"], 105, 108);
    expect(first?.map((r) => Number(r.index))).toEqual([105, 106, 107]);
    // The new file has other frames: the stale index would read local rows
    // 5-7, whose index values are 205-207, not 105-107.
    expect(await readParquetRowsByGlobalIndex(url, after, ["index"], 105, 108)).toBeNull();
    // The index was forgotten, so the next ask is located afresh.
    const again = await readParquetRowsByGlobalIndex(url, after, ["index"], 205, 208);
    expect(again?.map((r) => Number(r.index))).toEqual([205, 206, 207]);
  });

  test("returns null when the file has no index column", async () => {
    clearParquetFileCache();
    const file = fixtureBuffer();
    expect(
      await readParquetRowsByGlobalIndex(
        "fixture-noindex",
        file,
        ["value"],
        0,
        3,
        "nonexistent",
      ),
    ).toBeNull();
  });
});

describe("whole-file read cap", () => {
  const saved = { ...parquetLimits };
  afterEach(() => Object.assign(parquetLimits, saved));

  test("a full read above the cap fails before touching the file", async () => {
    parquetLimits.fullReadBytes = 1000;
    const file = fakeFile(2000);
    await expect(readParquetAsObjects(file)).rejects.toBeInstanceOf(
      ParquetTooLargeError,
    );
    expect(file.requests).toBe(0);
  });

  test("the error says what was refused and does not point at an environment variable", async () => {
    parquetLimits.fullReadBytes = 1000 * 1024;
    const error = await readParquetAsObjects(fakeFile(2000 * 1024)).catch(
      (e) => e,
    );
    expect(error.message).not.toContain("MAX_PARQUET");
    expect(error.message).toContain("about 2 MiB");
  });

  test("a full read at or below the cap proceeds", async () => {
    const file = fixtureBuffer();
    parquetLimits.fullReadBytes = file.byteLength;
    const rows = await readParquetAsObjects(file, ["index"]);
    expect(rows).toHaveLength(30);
  });

  test("a row range is allowed on a file above the cap", async () => {
    parquetLimits.fullReadBytes = 100;
    clearParquetFileCache();
    const file = fixtureBuffer();
    const rows = await readParquetAsObjects(file, ["index"], {
      rowStart: 0,
      rowEnd: 2,
    });
    expect(rows).toHaveLength(2);
  });

  test("selected row groups above the cap fail with the cap error", async () => {
    clearParquetFileCache();
    const file = fixtureBuffer();
    parquetLimits.fullReadBytes = 10; // bytes: smaller than any row group
    await expect(
      readParquetRowsByGlobalIndex(
        "fixture-cap",
        file,
        ["index", "value"],
        100,
        130,
      ),
    ).rejects.toBeInstanceOf(ParquetTooLargeError);
  });

  function serverIgnoringRange(options: {
    headLength: string | null;
    getLength: string | null;
    head?: number;
  }) {
    let cancelled = false;
    let bodyRequests = 0;
    const realFetch = globalThis.fetch;
    globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "HEAD") {
        const headers: Record<string, string> = {};
        if (options.headLength) headers["Content-Length"] = options.headLength;
        return new Response(null, { status: options.head ?? 200, headers });
      }
      bodyRequests += 1;
      const headers: Record<string, string> = {};
      if (options.getLength) headers["Content-Length"] = options.getLength;
      return new Response(
        new ReadableStream({
          pull(controller) {
            controller.enqueue(new Uint8Array(10));
            controller.close();
          },
          cancel() {
            cancelled = true;
          },
        }),
        { status: 200, headers },
      );
    }) as unknown as typeof fetch;
    return {
      restore: () => (globalThis.fetch = realFetch),
      cancelled: () => cancelled,
      bodyRequests: () => bodyRequests,
    };
  }

  test("without Content-Length on the 200, the length from HEAD still enforces the cap", async () => {
    clearParquetFileCache();
    parquetLimits.fullReadBytes = 100 * 1024;
    const server = serverIgnoringRange({
      headLength: "1000000",
      getLength: null,
    });
    try {
      const file = (await fetchParquetFile(
        "https://example.test/chunked.parquet",
      )) as AsyncBuffer;
      await expect(file.slice(0, 10)).rejects.toBeInstanceOf(
        ParquetTooLargeError,
      );
      expect(server.cancelled()).toBe(true);
      const stats = parquetCacheStats();
      expect(stats.metaBytes + stats.dataBytes).toBe(0);
    } finally {
      server.restore();
      clearParquetFileCache();
    }
  });

  test("without Content-Length and under the cap, the held body is counted from HEAD", async () => {
    clearParquetFileCache();
    parquetLimits.fullReadBytes = 2 * 1024 * 1024;
    parquetLimits.dataCacheBytes = 4 * 1024 * 1024;
    const server = serverIgnoringRange({
      headLength: "1000000",
      getLength: null,
    });
    try {
      const file = (await fetchParquetFile(
        "https://example.test/chunked-ok.parquet",
      )) as AsyncBuffer;
      await file.slice(0, 10);
      const stats = parquetCacheStats();
      expect(stats.metaBytes + stats.dataBytes).toBeGreaterThanOrEqual(1000000);
    } finally {
      server.restore();
      clearParquetFileCache();
    }
  });

  test("a 200 whose size is known nowhere is refused", async () => {
    clearParquetFileCache();
    // HEAD is forbidden (as with signed URLs): hyparquet falls back to a ranged GET.
    const server = serverIgnoringRange({
      headLength: null,
      getLength: null,
      head: 403,
    });
    try {
      await expect(
        fetchParquetFile("https://example.test/unknown.parquet"),
      ).rejects.toBeInstanceOf(ParquetTooLargeError);
      expect(server.cancelled()).toBe(true);
    } finally {
      server.restore();
      clearParquetFileCache();
    }
  });

  test("concurrent slices of a Range-ignoring server count its body once", async () => {
    clearParquetFileCache();
    parquetLimits.fullReadBytes = 2 * 1024 * 1024;
    parquetLimits.dataCacheBytes = 4 * 1024 * 1024;
    const server = serverIgnoringRange({
      headLength: "1000000",
      getLength: "1000000",
    });
    try {
      const file = (await fetchParquetFile(
        "https://example.test/concurrent.parquet",
      )) as AsyncBuffer;
      await Promise.all([
        file.slice(0, 10),
        file.slice(20, 30),
        file.slice(40, 50),
      ]);
      expect(server.bodyRequests()).toBeGreaterThan(0);
      const stats = parquetCacheStats();
      expect(stats.metaBytes + stats.dataBytes).toBeLessThan(2 * 1000000);
    } finally {
      server.restore();
      clearParquetFileCache();
    }
  });

  test("a server that ignores Range cannot make us download a huge file", async () => {
    clearParquetFileCache();
    parquetLimits.fullReadBytes = 1000;
    const realFetch = globalThis.fetch;
    let cancelled = false;
    globalThis.fetch = (async (_url: unknown, init?: RequestInit) => {
      if (init?.method === "HEAD") {
        return new Response(null, {
          status: 200,
          headers: { "Content-Length": "5000" },
        });
      }
      const body = new ReadableStream({
        pull(controller) {
          controller.enqueue(new Uint8Array(10));
        },
        cancel() {
          cancelled = true;
        },
      });
      return new Response(body, {
        status: 200,
        headers: { "Content-Length": "5000" },
      });
    }) as unknown as typeof fetch;
    try {
      const file = (await fetchParquetFile(
        "https://example.test/big.parquet",
      )) as AsyncBuffer;
      await expect(file.slice(0, 100)).rejects.toBeInstanceOf(
        ParquetTooLargeError,
      );
      expect(cancelled).toBe(true);
    } finally {
      globalThis.fetch = realFetch;
      clearParquetFileCache();
    }
  });
});
