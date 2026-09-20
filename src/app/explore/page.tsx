// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
import React from "react";
import ExploreGrid from "./explore-grid";
import { fetchJson, formatStringWithVars } from "@/utils/parquetUtils";
import { getDatasetVersion, buildVersionedUrl } from "@/utils/versionUtils";
import type { DatasetMetadata } from "@/utils/parquetUtils";

export default async function ExplorePage({
  searchParams,
}: {
  searchParams: Promise<{ p?: string; catalog?: string }>;
}) {
  const params = await searchParams;
  if (params.catalog !== "all") {
    // Public LeRobot datasets, checked reachable on 2026-09-20; keep in step
    // with DEMOS in levi/catalog.py. These are v3.0, whose video files live at
    // videos/<camera>/chunk-NNN/file-NNN.mp4 rather than the v2.x per-episode
    // path, so the preview is named per dataset instead of guessed.
    const demos = [
      {
        id: "lerobot/svla_so101_pickplace",
        camera: "observation.images.up",
      },
      {
        id: "lerobot/aloha_static_coffee",
        camera: "observation.images.cam_high",
      },
    ];
    return (
      <ExploreGrid
        datasets={demos.map(({ id, camera }) => ({
          id,
          videoUrl: `/api/proxy/datasets/${id}/resolve/main/videos/${camera}/chunk-000/file-000.mp4`,
        }))}
        currentPage={1}
        totalPages={1}
      />
    );
  }
  let datasets: { id: string }[] = [];
  let currentPage = 1;
  let totalPages = 1;
  try {
    const res = await fetch(
      "https://huggingface.co/api/datasets?sort=lastModified&filter=LeRobot",
      {
        cache: "no-store",
      },
    );
    if (!res.ok) throw new Error("Failed to fetch datasets");
    const data = await res.json();
    const allDatasets = data.datasets || data;
    // Use params from props
    const page = Math.max(1, parseInt(params?.p || "1", 10) || 1);
    const perPage = 30;

    totalPages = Math.max(1, Math.ceil(allDatasets.length / perPage));
    currentPage = Math.min(page, totalPages);

    const startIdx = (currentPage - 1) * perPage;
    const endIdx = startIdx + perPage;
    datasets = allDatasets.slice(startIdx, endIdx);
  } catch {
    return (
      <ExploreGrid
        datasets={[]}
        currentPage={1}
        totalPages={1}
        error="Failed to load datasets."
      />
    );
  }

  // Fetch episode 0 data for each dataset
  const datasetWithVideos = (
    await Promise.all(
      datasets.map(async (ds) => {
        try {
          const [org, dataset] = ds.id.split("/");
          const repoId = `${org}/${dataset}`;

          // Try to get compatible version, but don't fail the entire page if incompatible
          let version: string;
          try {
            version = await getDatasetVersion(repoId);
          } catch (err) {
            // Dataset is not compatible, skip it silently
            console.warn(
              `Skipping incompatible dataset ${repoId}: ${err instanceof Error ? err.message : err}`,
            );
            return null;
          }

          const jsonUrl = buildVersionedUrl(repoId, version, "meta/info.json");
          const info = await fetchJson<DatasetMetadata>(jsonUrl);
          const videoEntry = Object.entries(info.features).find(
            ([, value]) => value.dtype === "video",
          );
          let videoUrl: string | null = null;
          if (videoEntry && info.video_path) {
            const [key] = videoEntry;
            const videoPath = formatStringWithVars(info.video_path, {
              video_key: key,
              episode_chunk: "0".padStart(3, "0"),
              episode_index: "0".padStart(6, "0"),
            });
            const url = buildVersionedUrl(repoId, version, videoPath);
            // Check if videoUrl exists (status 200)
            try {
              const headRes = await fetch(url, { method: "HEAD" });
              if (headRes.ok) {
                videoUrl = url;
              }
            } catch {
              // If fetch fails, videoUrl remains null
            }
          }
          return { id: repoId, videoUrl };
        } catch (err) {
          console.error(
            `Failed to fetch or parse dataset info for ${ds.id}:`,
            err,
          );
          return null;
        }
      }),
    )
  ).filter(Boolean) as { id: string; videoUrl: string | null }[];

  return (
    <ExploreGrid
      datasets={datasetWithVideos}
      currentPage={currentPage}
      totalPages={totalPages}
    />
  );
}
