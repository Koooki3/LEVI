// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.
"use client";
import { EpisodeLoadError } from "@/components/viewer/load-error";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return <EpisodeLoadError message={error.message} onRetry={reset} />;
}
