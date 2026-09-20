import { describe, expect, test, mock, afterEach } from "bun:test";
import {
  buildVersionedUrl,
  describeInfoFailure,
  normalizeDatasetVersion,
  SUPPORTED_DATASET_VERSIONS,
} from "@/utils/versionUtils";
import { authHeaders } from "@/utils/auth";

// ---------------------------------------------------------------------------
// buildVersionedUrl — pure function, no mocking needed
// ---------------------------------------------------------------------------
describe("buildVersionedUrl", () => {
  test("builds URL for v2.0 dataset data path", () => {
    const url = buildVersionedUrl(
      "rabhishek100/so100_train_dataset",
      "v2.0",
      "data/000/episode_000000.parquet",
    );
    expect(url).toBe(
      "https://huggingface.co/datasets/rabhishek100/so100_train_dataset/resolve/main/data/000/episode_000000.parquet",
    );
  });

  test("builds URL for v2.1 dataset video path", () => {
    const url = buildVersionedUrl(
      "youliangtan/so101-table-cleanup",
      "v2.1",
      "videos/observation.images.top/chunk-000/episode_000007.mp4",
    );
    expect(url).toBe(
      "https://huggingface.co/datasets/youliangtan/so101-table-cleanup/resolve/main/videos/observation.images.top/chunk-000/episode_000007.mp4",
    );
  });

  test("builds URL for v3.0 episode metadata", () => {
    const url = buildVersionedUrl(
      "lerobot-data-collection/level12_rac_2_2026-02-07",
      "v3.0",
      "meta/episodes/chunk-000/file-000.parquet",
    );
    expect(url).toBe(
      "https://huggingface.co/datasets/lerobot-data-collection/level12_rac_2_2026-02-07/resolve/main/meta/episodes/chunk-000/file-000.parquet",
    );
  });

  test("builds URL for v3.0 data chunk", () => {
    const url = buildVersionedUrl(
      "lerobot-data-collection/level12_rac_2_2026-02-07",
      "v3.0",
      "data/chunk-001/file-003.parquet",
    );
    expect(url).toBe(
      "https://huggingface.co/datasets/lerobot-data-collection/level12_rac_2_2026-02-07/resolve/main/data/chunk-001/file-003.parquet",
    );
  });

  test("builds URL for meta/info.json", () => {
    const url = buildVersionedUrl("myorg/mydataset", "v3.0", "meta/info.json");
    expect(url).toBe(
      "https://huggingface.co/datasets/myorg/mydataset/resolve/main/meta/info.json",
    );
  });
});

describe("normalizeDatasetVersion", () => {
  test("canonicalizes common LeRobot aliases", () => {
    expect(normalizeDatasetVersion("3.0")).toBe("v3.0");
    expect(normalizeDatasetVersion("v3.1.0")).toBe("v3.1");
    expect(normalizeDatasetVersion(" V2.1 ")).toBe("v2.1");
  });

  test("rejects unsupported or incomplete versions", () => {
    expect(normalizeDatasetVersion("v1.0")).toBeNull();
    expect(normalizeDatasetVersion("v3")).toBeNull();
    expect(SUPPORTED_DATASET_VERSIONS).toContain("v3.1");
  });
});

// ---------------------------------------------------------------------------
// getDatasetVersionAndInfo — tested with mocked fetch
// ---------------------------------------------------------------------------
describe("getDatasetVersionAndInfo", () => {
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  test("accepts v2.0 codebase_version", async () => {
    const infoV20 = {
      codebase_version: "v2.0",
      robot_type: "so100",
      total_episodes: 50,
      total_frames: 5000,
      total_tasks: 1,
      chunks_size: 1000,
      data_files_size_in_mb: 10,
      video_files_size_in_mb: 500,
      fps: 30,
      splits: { train: "0:50" },
      data_path: "data/{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
      video_path:
        "videos/{video_key}/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.mp4",
      features: {
        "observation.images.top": {
          dtype: "video",
          shape: [480, 640, 3],
          names: null,
        },
        "observation.state": {
          dtype: "float32",
          shape: [1, 6],
          names: ["j0", "j1", "j2", "j3", "j4", "j5"],
        },
        action: {
          dtype: "float32",
          shape: [1, 6],
          names: ["j0", "j1", "j2", "j3", "j4", "j5"],
        },
      },
    };

    globalThis.fetch = mock(() =>
      Promise.resolve(new Response(JSON.stringify(infoV20), { status: 200 })),
    ) as unknown as typeof fetch;

    const { getDatasetVersionAndInfo } = await import("@/utils/versionUtils");
    const result = await getDatasetVersionAndInfo(
      "rabhishek100/so100_train_dataset",
    );
    expect(result.version).toBe("v2.0");
    expect(result.info.total_episodes).toBe(50);
  });

  test("accepts v2.1 codebase_version", async () => {
    const infoV21 = {
      codebase_version: "v2.1",
      robot_type: "so101",
      total_episodes: 100,
      total_frames: 10000,
      total_tasks: 1,
      chunks_size: 1000,
      data_files_size_in_mb: 20,
      video_files_size_in_mb: 1000,
      fps: 30,
      splits: { train: "0:100" },
      data_path: "data/{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
      video_path:
        "videos/{video_key}/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.mp4",
      features: {
        "observation.images.top": {
          dtype: "video",
          shape: [480, 640, 3],
          names: null,
        },
        "observation.state": { dtype: "float32", shape: [1, 6], names: null },
        action: { dtype: "float32", shape: [1, 6], names: null },
      },
    };

    globalThis.fetch = mock(() =>
      Promise.resolve(new Response(JSON.stringify(infoV21), { status: 200 })),
    ) as unknown as typeof fetch;

    // Use fresh import to bypass cache — or just call with a different repoId
    const { getDatasetVersionAndInfo } = await import("@/utils/versionUtils");
    const result = await getDatasetVersionAndInfo(
      "youliangtan/so101-table-cleanup",
    );
    expect(result.version).toBe("v2.1");
  });

  test("accepts v3.0 codebase_version", async () => {
    const infoV30 = {
      codebase_version: "v3.0",
      robot_type: "openarm",
      total_episodes: 200,
      total_frames: 40000,
      total_tasks: 1,
      chunks_size: 100,
      data_files_size_in_mb: 50,
      video_files_size_in_mb: 2000,
      fps: 50,
      splits: { train: "0:200" },
      data_path: null,
      video_path: null,
      features: {
        "observation.images.top": {
          dtype: "video",
          shape: [480, 640, 3],
          names: null,
        },
        "observation.state": { dtype: "float32", shape: [1, 14], names: null },
        action: { dtype: "float32", shape: [1, 14], names: null },
      },
    };

    globalThis.fetch = mock(() =>
      Promise.resolve(new Response(JSON.stringify(infoV30), { status: 200 })),
    ) as unknown as typeof fetch;

    const { getDatasetVersionAndInfo } = await import("@/utils/versionUtils");
    const result = await getDatasetVersionAndInfo(
      "lerobot-data-collection/level12_rac_2_2026-02-07",
    );
    expect(result.version).toBe("v3.0");
    expect(result.info.total_episodes).toBe(200);
  });

  test("canonicalizes an unprefixed version and normalizes numeric metadata", async () => {
    globalThis.fetch = mock(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            codebase_version: "3.0.0",
            total_episodes: "4",
            total_frames: "40",
            total_tasks: "2",
            fps: "10",
            chunks_size: "2",
            data_path: null,
            video_path: null,
            features: {
              action: { dtype: "float32", shape: [2], names: null },
            },
          }),
          { status: 200 },
        ),
      ),
    ) as unknown as typeof fetch;

    const { getDatasetVersionAndInfo } = await import("@/utils/versionUtils");
    const result = await getDatasetVersionAndInfo("aliases/v30");
    expect(result.version).toBe("v3.0");
    expect(result.info.codebase_version).toBe("v3.0");
    expect(result.info.total_tasks).toBe(2);
    expect(result.info.total_episodes).toBe(4);
  });

  test("throws for unsupported version", async () => {
    const infoUnsupported = {
      codebase_version: "v1.0",
      features: { dummy: { dtype: "float32", shape: [1], names: null } },
    };

    globalThis.fetch = mock(() =>
      Promise.resolve(
        new Response(JSON.stringify(infoUnsupported), { status: 200 }),
      ),
    ) as unknown as typeof fetch;

    const { getDatasetVersionAndInfo } = await import("@/utils/versionUtils");
    await expect(getDatasetVersionAndInfo("old/dataset")).rejects.toThrow(
      "not supported",
    );
  });

  test("throws when info.json has no features field", async () => {
    globalThis.fetch = mock(() =>
      Promise.resolve(
        new Response(JSON.stringify({ codebase_version: "v3.0" }), {
          status: 200,
        }),
      ),
    ) as unknown as typeof fetch;

    const { getDatasetVersionAndInfo } = await import("@/utils/versionUtils");
    await expect(getDatasetVersionAndInfo("broken/dataset")).rejects.toThrow();
  });

  test("throws when fetch fails (network error)", async () => {
    globalThis.fetch = mock(() =>
      Promise.resolve(new Response("Not Found", { status: 404 })),
    ) as unknown as typeof fetch;

    const { getDatasetVersionAndInfo } = await import("@/utils/versionUtils");
    await expect(
      getDatasetVersionAndInfo("nonexistent/repo"),
    ).rejects.toThrow();
  });
});

describe("describeInfoFailure", () => {
  test("tells a removed Hub dataset apart from a login problem", () => {
    const message = describeInfoFailure("samanthalhy/so100_strawberry_2", 401);
    expect(message).toContain("samanthalhy/so100_strawberry_2");
    expect(message).toContain("private or no longer exists");
    expect(message).toContain("Sign in");
  });

  test("points a local failure at the workspace, not at credentials", () => {
    const message = describeInfoFailure("local/my_capture", 404);
    expect(message).toContain("local dataset my_capture");
    expect(message).toContain("meta/info.json");
    expect(message).not.toContain("Sign in");
  });

  test("still reports an unexpected status verbatim", () => {
    expect(describeInfoFailure("org/name", 503)).toContain("503");
  });
});

describe("authHeaders", () => {
  const backend = "http://127.0.0.1:7861";
  afterEach(() => {
    delete process.env.LEVI_BACKEND_URL;
    delete process.env.LEVI_UI_TOKEN;
  });

  test("authorises server-side reads of LEVI's own file service", () => {
    process.env.LEVI_BACKEND_URL = backend;
    process.env.LEVI_UI_TOKEN = "secret-ui-token";
    const headers = authHeaders(
      `${backend}/api/levi/files/capture/meta/info.json`,
    );
    expect(headers["x-levi-ui-token"]).toBe("secret-ui-token");
  });

  test("never sends LEVI's internal token to the Hub", () => {
    process.env.LEVI_BACKEND_URL = backend;
    process.env.LEVI_UI_TOKEN = "secret-ui-token";
    const headers = authHeaders(
      "https://huggingface.co/datasets/org/name/resolve/main/meta/info.json",
    );
    expect(headers["x-levi-ui-token"]).toBeUndefined();
  });

  test("never sends a Hugging Face credential to LEVI's own service", () => {
    // Signing in to the Hub must not change what LEVI's loopback API receives.
    const stored = JSON.stringify({ accessToken: "hf_example_token" });
    const storage = {
      getItem: () => stored,
      setItem: () => {},
      removeItem: () => {},
    };
    const original = (globalThis as { window?: unknown }).window;
    Object.defineProperty(globalThis, "window", {
      value: { localStorage: storage, sessionStorage: storage },
      configurable: true,
    });
    try {
      expect(
        authHeaders("/api/levi/files/capture/meta/info.json").Authorization,
      ).toBeUndefined();
      expect(
        authHeaders("/annotations/api/episodes/0/atoms").Authorization,
      ).toBeUndefined();
      // The Hub still gets it.
      expect(
        authHeaders("https://huggingface.co/datasets/org/name/resolve/main/x")
          .Authorization,
      ).toBe("Bearer hf_example_token");
    } finally {
      Object.defineProperty(globalThis, "window", {
        value: original,
        configurable: true,
      });
    }
  });

  test("adds nothing when no URL is given", () => {
    process.env.LEVI_BACKEND_URL = backend;
    process.env.LEVI_UI_TOKEN = "secret-ui-token";
    expect(authHeaders()["x-levi-ui-token"]).toBeUndefined();
  });
});
