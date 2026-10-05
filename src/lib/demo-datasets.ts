/**
 * The two public demo datasets (keep in step with DEMOS in levi/catalog.py)
 * and where each one's first-frame video is. They are v3.0 datasets, whose
 * video files live at `videos/<camera>/chunk-NNN/file-NNN.mp4`, not at the v2.x
 * per-episode path, so the camera is named per dataset instead of guessed.
 * One definition for the Explore page and the guide.
 */
export const DEMO_DATASETS = [
  { id: "lerobot/svla_so101_pickplace", camera: "observation.images.up" },
  {
    id: "lerobot/aloha_static_coffee",
    camera: "observation.images.cam_high",
  },
] as const;

/** The proxied URL of a demo dataset's first video file, or null for a
 * dataset that is not a known demo. */
export function demoVideoUrl(id: string): string | null {
  const demo = DEMO_DATASETS.find((item) => item.id === id);
  return demo
    ? `/api/proxy/datasets/${demo.id}/resolve/main/videos/${demo.camera}/chunk-000/file-000.mp4`
    : null;
}
